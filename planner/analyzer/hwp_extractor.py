"""HWP (한컴 구형, HWP5) → 평문 텍스트 추출.

HWP5 는 OLE2 (Compound Document) 컨테이너 안에 한컴 전용 바이너리 스트림.
직접 파싱은 매우 복잡하므로 pyhwp 의 hwp5txt CLI 를 subprocess 로 호출한다.

특징:
- HWP5 호환 파일만 지원 (정부 공고 대부분은 HWP5 호환)
- 본문 텍스트만 추출 (표·그림 정보 손실)
- 외부 의존성: pyhwp (pip install). CLI hwp5txt 가 PATH 에 추가됨.

추출 흐름:
  hwp_bytes → 임시 파일 → `hwp5txt` 실행 → stdout 평문 → 임시 파일 삭제
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

# OLE2 Compound Document 매직 바이트 — HWP5 / DOC / XLS 등 모두 동일.
# (D0 CF 11 E0 A1 B1 1A E1 = "DocFile" ASCII art)
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _find_hwp5txt() -> str | list[str]:
    """hwp5txt CLI 위치 탐색. systemd 가 venv 안에서 실행돼도 찾도록 다단 fallback.

    우선순위:
      1. 현재 Python (sys.executable) 와 같은 디렉터리의 hwp5txt
         → venv 환경이면 venv/bin/hwp5txt 가 여기 있음
      2. PATH 에서 hwp5txt 찾기
      3. fallback: python -m hwp5.hwp5txt (모듈 실행)
    """
    # 1) sys.executable 디렉터리
    py_bin_dir = os.path.dirname(sys.executable)
    for name in ("hwp5txt", "hwp5txt.exe"):
        candidate = os.path.join(py_bin_dir, name)
        if os.path.isfile(candidate):
            return candidate
    # 2) PATH
    found = shutil.which("hwp5txt")
    if found:
        return found
    # 3) 모듈 실행 fallback (pyhwp 에 hwp5.hwp5txt 모듈 존재 시)
    return [sys.executable, "-m", "hwp5.hwp5txt"]


# 모듈 로드 시 1회만 탐색해 캐시
_HWP5TXT_CMD = _find_hwp5txt()


def is_hwp_bytes(data: bytes) -> bool:
    """매직 바이트로 HWP5(=OLE2) 가능성 확인.

    OLE2 컨테이너는 DOC/XLS 도 같은 매직이라 100% 확정은 아님.
    실제 HWP 인지는 hwp5txt 실행 결과로 판단.
    """
    return data.startswith(_OLE2_MAGIC)


def extract_hwp_text(hwp_bytes: bytes, *, timeout: int = 30) -> str:
    """HWP5 바이트 → 평문 텍스트.

    Raises:
      ValueError: 매직 바이트 불일치 / hwp5txt 실행 실패 / timeout
    """
    if not is_hwp_bytes(hwp_bytes):
        raise ValueError("HWP5(OLE2) 매직 바이트 불일치 — 손상되었거나 다른 포맷")

    # 임시 파일 작성 (Windows 의 NamedTemporaryFile 이슈 회피 위해 mkstemp 사용)
    fd, tmp_path = tempfile.mkstemp(suffix=".hwp", prefix="ideaRO_hwp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(hwp_bytes)
        # _HWP5TXT_CMD 는 str (실행파일 경로) 또는 list (python -m fallback).
        if isinstance(_HWP5TXT_CMD, list):
            cmd = _HWP5TXT_CMD + [tmp_path]
        else:
            cmd = [_HWP5TXT_CMD, tmp_path]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise ValueError(
                "hwp5txt 명령을 찾을 수 없습니다 — `pip install pyhwp` 가 필요합니다. "
                "venv 사용 시 그 venv 안에 설치해주세요."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ValueError(f"hwp5txt 실행이 {timeout}초 안에 끝나지 않았습니다") from e

        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace")[:300]
            raise ValueError(f"hwp5txt 실패 (rc={result.returncode}): {err}")

        # 출력은 보통 UTF-8 이지만 일부 환경에서 cp949 로 나올 수 있어 fallback.
        raw = result.stdout or b""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp949", errors="replace")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
