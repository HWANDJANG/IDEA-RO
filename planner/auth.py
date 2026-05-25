"""카카오 OAuth + 세션 쿠키 (HMAC 서명) 모듈.

쿠키 포맷:
    fl_session = {user_id}.{base64url(hmac_sha256(user_id, SESSION_SECRET))}

- HttpOnly + SameSite=Lax 로 발급
- DB 의 session 테이블 없이, 쿠키 자체가 유일한 인증 토큰 (stateless)
- 위조 방지: SESSION_SECRET 모르면 새 user_id 로 위조 불가
- 만료: Max-Age=30일 (재방문 시 자동 갱신)

흐름:
    1. /api/auth/kakao/login   → 카카오 동의 화면으로 302
    2. /api/auth/kakao/callback?code=...
         a. code → access_token  (kauth.kakao.com/oauth/token)
         b. access_token → user info  (kapi.kakao.com/v2/user/me)
         c. users 테이블 upsert
         d. 세션 쿠키 발급 + 302 redirect /
    3. /api/auth/me   → 현재 사용자 정보 (없으면 null)
    4. /api/auth/logout → 쿠키 만료
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from planner.paths import DB_PATH


COOKIE_NAME = "fl_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30일


# ─── 환경변수 로드 ──────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def kakao_client_id() -> str:
    return _env("KAKAO_REST_API_KEY")


def kakao_redirect_uri() -> str:
    return _env("KAKAO_REDIRECT_URI", "http://localhost:8765/api/auth/kakao/callback")


def kakao_client_secret() -> str:
    """선택 사항. 카카오 콘솔에서 Client Secret 사용함 으로 설정한 경우만 필요."""
    return _env("KAKAO_CLIENT_SECRET")


def session_secret() -> str:
    s = _env("SESSION_SECRET")
    if not s:
        raise RuntimeError("SESSION_SECRET 환경변수가 설정되지 않았습니다 (.env 확인)")
    return s


# ─── 세션 쿠키 (서명) ──────────────────────────────────────────────────
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_user_id(user_id: int) -> str:
    """user_id 를 HMAC 서명해서 쿠키 값으로 변환."""
    payload = str(user_id)
    sig = hmac.new(
        session_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{payload}.{_b64url_encode(sig)}"


def verify_session_cookie(cookie_value: Optional[str]) -> Optional[int]:
    """쿠키 검증 후 user_id (int) 반환. 위조/만료 시 None."""
    if not cookie_value or "." not in cookie_value:
        return None
    try:
        payload, sig_b64 = cookie_value.rsplit(".", 1)
        expected_sig = hmac.new(
            session_secret().encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        provided_sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None
        return int(payload)
    except (ValueError, TypeError):
        return None


def parse_cookies(cookie_header: Optional[str]) -> dict[str, str]:
    """'a=1; b=2' → {'a': '1', 'b': '2'}"""
    result: dict[str, str] = {}
    if not cookie_header:
        return result
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def make_set_cookie_header(user_id: int) -> str:
    """로그인 성공 시 발급할 Set-Cookie 헤더 값."""
    value = sign_user_id(user_id)
    parts = [
        f"{COOKIE_NAME}={value}",
        "Path=/",
        f"Max-Age={COOKIE_MAX_AGE}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    # 로컬 개발 (http) 에서는 Secure 안 붙임. 실배포 (https) 에서는 붙여야 함.
    if _env("FORCE_SECURE_COOKIE", "").lower() in ("1", "true", "yes"):
        parts.append("Secure")
    return "; ".join(parts)


def make_clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


# ─── 카카오 OAuth ─────────────────────────────────────────────────────
def kakao_authorize_url(state: str | None = None) -> str:
    """카카오 동의 화면 URL 생성. state 는 로그인 성공 후 돌아갈 내부 경로 등 round-trip 용."""
    client_id = kakao_client_id()
    if not client_id:
        raise RuntimeError("KAKAO_REST_API_KEY 가 .env 에 설정되지 않았습니다")
    params = {
        "client_id": client_id,
        "redirect_uri": kakao_redirect_uri(),
        "response_type": "code",
    }
    if state:
        params["state"] = state
    return "https://kauth.kakao.com/oauth/authorize?" + urllib.parse.urlencode(params)


def exchange_code_for_token(code: str) -> dict:
    """authorization code → access_token. 표준 라이브러리로 POST.

    카카오 콘솔에서 Client Secret 사용함 으로 설정한 경우 KAKAO_CLIENT_SECRET 도 함께 전송.
    """
    params = {
        "grant_type": "authorization_code",
        "client_id": kakao_client_id(),
        "redirect_uri": kakao_redirect_uri(),
        "code": code,
    }
    secret = kakao_client_secret()
    if secret:
        params["client_secret"] = secret

    payload = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 카카오 에러 응답 본문도 같이 노출 (예: {"error":"invalid_grant",...})
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Kakao token endpoint {e.code}: {body}") from e


def fetch_kakao_user_info(access_token: str) -> dict:
    """access_token 으로 사용자 정보 조회."""
    req = urllib.request.Request(
        "https://kapi.kakao.com/v2/user/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Kakao userinfo endpoint {e.code}: {body}") from e


# ─── DB ──────────────────────────────────────────────────────────────
def upsert_user_from_kakao(info: dict) -> int:
    """kakao 사용자 정보를 받아 users 테이블 upsert 후 user.id 반환."""
    kakao_id = str(info.get("id"))
    if not kakao_id:
        raise RuntimeError("Kakao 응답에 id 가 없습니다")

    account = info.get("kakao_account") or {}
    profile = account.get("profile") or {}
    nickname = profile.get("nickname")
    email = account.get("email") if account.get("is_email_valid") else None
    profile_img = profile.get("profile_image_url")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users(auth_provider, provider_uid, nickname, email, profile_img, last_login_at) "
            "VALUES ('kakao', ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(auth_provider, provider_uid) DO UPDATE SET "
            "  nickname=excluded.nickname, "
            "  email=COALESCE(excluded.email, users.email), "
            "  profile_img=COALESCE(excluded.profile_img, users.profile_img), "
            "  last_login_at=CURRENT_TIMESTAMP",
            (kakao_id, nickname, email, profile_img),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM users WHERE auth_provider='kakao' AND provider_uid=?",
            (kakao_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("user upsert 실패")
        return int(row[0])
    finally:
        conn.close()


def load_user(user_id: int) -> Optional[dict]:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, auth_provider, provider_uid, nickname, email, profile_img, "
            "       created_at, last_login_at "
            "FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ─── Google OAuth ─────────────────────────────────────────────────────
def google_client_id() -> str:
    return _env("GOOGLE_CLIENT_ID")


def google_client_secret() -> str:
    return _env("GOOGLE_CLIENT_SECRET")


def google_redirect_uri() -> str:
    return _env("GOOGLE_REDIRECT_URI", "http://localhost:8765/api/auth/google/callback")


GOOGLE_BASE_SCOPES = "openid email profile"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def google_authorize_url(
    state: str | None = None,
    *,
    include_calendar: bool = False,
) -> str:
    """Google 동의 화면 URL 생성.

    include_calendar=True 면 캘린더 쓰기 권한도 동시에 요청. refresh_token 까지 받기 위해
    `access_type=offline` + `prompt=consent` 사용.
    """
    client_id = google_client_id()
    if not client_id:
        raise RuntimeError("GOOGLE_CLIENT_ID 가 .env 에 설정되지 않았습니다")
    scopes = GOOGLE_BASE_SCOPES
    if include_calendar:
        scopes = scopes + " " + GOOGLE_CALENDAR_SCOPE
    params = {
        "client_id": client_id,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": scopes,
        # 캘린더 권한 받으려면 refresh_token 도 필요 → offline + consent
        "access_type": "offline" if include_calendar else "online",
        "prompt": "consent" if include_calendar else "select_account",
        # incremental authorization — 기존 동의된 스코프 유지
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange_google_code_for_token(code: str) -> dict:
    """authorization code → access_token + id_token."""
    client_id = google_client_id()
    client_secret = google_client_secret()
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 가 .env 에 설정되지 않았습니다")
    params = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": google_redirect_uri(),
        "code": code,
    }
    payload = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Google token endpoint {e.code}: {body}") from e


def fetch_google_user_info(access_token: str) -> dict:
    """access_token 으로 사용자 정보 조회 (openid userinfo)."""
    req = urllib.request.Request(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Google userinfo endpoint {e.code}: {body}") from e


def upsert_user_from_google(info: dict) -> int:
    """Google userinfo dict 를 받아 users 테이블 upsert 후 user.id 반환.

    info 키: sub, name, email, email_verified, picture, locale 등
    """
    google_id = str(info.get("sub") or "")
    if not google_id:
        raise RuntimeError("Google 응답에 sub(고유 ID) 가 없습니다")

    nickname = info.get("name")
    email = info.get("email") if info.get("email_verified") else None
    profile_img = info.get("picture")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users(auth_provider, provider_uid, nickname, email, profile_img, last_login_at) "
            "VALUES ('google', ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(auth_provider, provider_uid) DO UPDATE SET "
            "  nickname=excluded.nickname, "
            "  email=COALESCE(excluded.email, users.email), "
            "  profile_img=COALESCE(excluded.profile_img, users.profile_img), "
            "  last_login_at=CURRENT_TIMESTAMP",
            (google_id, nickname, email, profile_img),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM users WHERE auth_provider='google' AND provider_uid=?",
            (google_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("user upsert 실패")
        return int(row[0])
    finally:
        conn.close()


# ─── Google Calendar 토큰 저장/관리 ──────────────────────────────────────
def _now_iso_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_utc_after(seconds: int) -> str:
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def save_google_calendar_tokens(
    user_id: int,
    *,
    refresh_token: Optional[str],
    access_token: str,
    expires_in: int,
    scopes: str,
) -> None:
    """캘린더 연동 동의 후 받은 토큰들을 google_tokens 에 upsert.

    refresh_token 은 첫 동의 시에만 주어지므로, 없으면 기존 값 유지.
    """
    expires_at = _iso_utc_after(max(0, expires_in - 30))  # 30초 안전 마진
    conn = sqlite3.connect(DB_PATH)
    try:
        if refresh_token:
            conn.execute(
                "INSERT INTO google_tokens(user_id, refresh_token, access_token, expires_at, scopes, updated_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "  refresh_token=excluded.refresh_token, "
                "  access_token=excluded.access_token, "
                "  expires_at=excluded.expires_at, "
                "  scopes=excluded.scopes, "
                "  updated_at=CURRENT_TIMESTAMP",
                (user_id, refresh_token, access_token, expires_at, scopes),
            )
        else:
            # refresh_token 없음 → 기존 refresh_token 유지하면서 access_token 만 업데이트
            conn.execute(
                "UPDATE google_tokens SET access_token=?, expires_at=?, scopes=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE user_id=?",
                (access_token, expires_at, scopes, user_id),
            )
        conn.commit()
    finally:
        conn.close()


def load_google_tokens(user_id: int) -> Optional[dict]:
    """google_tokens 행 로드. 없으면 None."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT user_id, refresh_token, access_token, expires_at, scopes "
            "FROM google_tokens WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_google_tokens(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM google_tokens WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def refresh_google_access_token(refresh_token: str) -> dict:
    """refresh_token 으로 새 access_token 발급. {access_token, expires_in, scope, ...} 반환."""
    client_id = google_client_id()
    client_secret = google_client_secret()
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 가 .env 에 설정되지 않았습니다")
    params = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    payload = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Google token refresh {e.code}: {body}") from e


def get_valid_google_access_token(user_id: int) -> Optional[str]:
    """캘린더 API 호출용 유효 access_token 반환. 만료됐으면 refresh 후 새 토큰 저장하고 반환.
    연동 안돼있거나 refresh 실패 시 None."""
    from datetime import datetime, timezone
    tokens = load_google_tokens(user_id)
    if not tokens:
        return None
    expires_at = tokens.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp > datetime.now(timezone.utc):
                return tokens.get("access_token")
        except ValueError:
            pass
    # 만료 → refresh
    rt = tokens.get("refresh_token")
    if not rt:
        return None
    try:
        resp = refresh_google_access_token(rt)
    except RuntimeError as e:
        # refresh_token 자체가 무효 (사용자가 동의 철회) → 삭제하고 None
        if "invalid_grant" in str(e):
            delete_google_tokens(user_id)
        return None
    access = resp.get("access_token")
    if not access:
        return None
    save_google_calendar_tokens(
        user_id,
        refresh_token=None,  # refresh 응답엔 refresh_token 안 들어옴 → 기존 유지
        access_token=access,
        expires_in=int(resp.get("expires_in") or 3600),
        scopes=resp.get("scope") or tokens.get("scopes") or "",
    )
    return access


def has_calendar_scope(user_id: int) -> bool:
    tokens = load_google_tokens(user_id)
    if not tokens:
        return False
    scopes = tokens.get("scopes") or ""
    return GOOGLE_CALENDAR_SCOPE in scopes


# ─── Naver OAuth + Calendar ───────────────────────────────────────────
# 네이버는 OAuth scope 가 별도로 없음 — 앱 등록 시 "캘린더 API" 사용 신청만 하면
# 로그인 동의 한 번으로 캘린더 호출 가능. refresh_token 으로 영구 사용.
def naver_client_id() -> str:
    return _env("NAVER_CLIENT_ID")


def naver_client_secret() -> str:
    return _env("NAVER_CLIENT_SECRET")


def naver_redirect_uri() -> str:
    return _env("NAVER_REDIRECT_URI", "http://localhost:8765/api/auth/naver/callback")


def naver_authorize_url(state: str | None = None, *, force_reconsent: bool = False) -> str:
    client_id = naver_client_id()
    if not client_id:
        raise RuntimeError("NAVER_CLIENT_ID 가 .env 에 설정되지 않았습니다")
    # 네이버는 state 가 CSRF 방어 + round-trip 정보 양쪽으로 사용됨 (필수)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": naver_redirect_uri(),
        "state": state or "/",
    }
    # 앱에 API 가 새로 추가된 경우, 기존 동의를 재요구하려면 auth_type=reauthenticate
    if force_reconsent:
        params["auth_type"] = "reauthenticate"
    return "https://nid.naver.com/oauth2.0/authorize?" + urllib.parse.urlencode(params)


def exchange_naver_code_for_token(code: str, state: str) -> dict:
    """authorization code → access_token + refresh_token."""
    client_id = naver_client_id()
    client_secret = naver_client_secret()
    if not client_id or not client_secret:
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 .env 에 설정되지 않았습니다")
    params = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "state": state,
    }
    payload = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        "https://nid.naver.com/oauth2.0/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Naver token endpoint {e.code}: {body}") from e


def fetch_naver_user_info(access_token: str) -> dict:
    """access_token 으로 사용자 정보 조회. 응답: {resultcode, message, response: {id, email, name, ...}}"""
    req = urllib.request.Request(
        "https://openapi.naver.com/v1/nid/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Naver userinfo endpoint {e.code}: {body}") from e


def upsert_user_from_naver(info: dict) -> int:
    """네이버 me API 응답 → users 테이블 upsert.

    info 형식: {"resultcode": "00", "message": "success",
                 "response": {"id": "...", "email": "...", "name": "...", "nickname": "...", "profile_image": "..."}}
    """
    resp = info.get("response") or {}
    naver_id = str(resp.get("id") or "")
    if not naver_id:
        raise RuntimeError("Naver 응답에 id 가 없습니다")

    nickname = resp.get("nickname") or resp.get("name")
    email = resp.get("email")
    profile_img = resp.get("profile_image")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users(auth_provider, provider_uid, nickname, email, profile_img, last_login_at) "
            "VALUES ('naver', ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(auth_provider, provider_uid) DO UPDATE SET "
            "  nickname=excluded.nickname, "
            "  email=COALESCE(excluded.email, users.email), "
            "  profile_img=COALESCE(excluded.profile_img, users.profile_img), "
            "  last_login_at=CURRENT_TIMESTAMP",
            (naver_id, nickname, email, profile_img),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM users WHERE auth_provider='naver' AND provider_uid=?",
            (naver_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("user upsert 실패")
        return int(row[0])
    finally:
        conn.close()


def save_naver_tokens(
    user_id: int,
    *,
    refresh_token: Optional[str],
    access_token: str,
    expires_in: int,
) -> None:
    expires_at = _iso_utc_after(max(0, expires_in - 30))
    conn = sqlite3.connect(DB_PATH)
    try:
        if refresh_token:
            conn.execute(
                "INSERT INTO naver_tokens(user_id, refresh_token, access_token, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "  refresh_token=excluded.refresh_token, "
                "  access_token=excluded.access_token, "
                "  expires_at=excluded.expires_at, "
                "  updated_at=CURRENT_TIMESTAMP",
                (user_id, refresh_token, access_token, expires_at),
            )
        else:
            conn.execute(
                "UPDATE naver_tokens SET access_token=?, expires_at=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE user_id=?",
                (access_token, expires_at, user_id),
            )
        conn.commit()
    finally:
        conn.close()


def load_naver_tokens(user_id: int) -> Optional[dict]:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT user_id, refresh_token, access_token, expires_at "
            "FROM naver_tokens WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_naver_tokens(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM naver_tokens WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def refresh_naver_access_token(refresh_token: str) -> dict:
    client_id = naver_client_id()
    client_secret = naver_client_secret()
    if not client_id or not client_secret:
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 .env 에 설정되지 않았습니다")
    params = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    payload = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        "https://nid.naver.com/oauth2.0/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Naver token refresh {e.code}: {body}") from e


def get_valid_naver_access_token(user_id: int) -> Optional[str]:
    from datetime import datetime, timezone
    tokens = load_naver_tokens(user_id)
    if not tokens:
        return None
    expires_at = tokens.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp > datetime.now(timezone.utc):
                return tokens.get("access_token")
        except ValueError:
            pass
    rt = tokens.get("refresh_token")
    if not rt:
        return None
    try:
        resp = refresh_naver_access_token(rt)
    except RuntimeError as e:
        if "invalid_grant" in str(e) or "invalid_request" in str(e):
            delete_naver_tokens(user_id)
        return None
    access = resp.get("access_token")
    if not access:
        return None
    save_naver_tokens(
        user_id,
        refresh_token=resp.get("refresh_token"),  # 네이버는 갱신 시 새 refresh_token 도 줄 수 있음
        access_token=access,
        expires_in=int(resp.get("expires_in") or 3600),
    )
    return access


def has_naver_calendar(user_id: int) -> bool:
    """네이버는 별도 scope 가 없으니 토큰이 있으면 캘린더 가능."""
    return load_naver_tokens(user_id) is not None
