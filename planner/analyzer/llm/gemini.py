"""Google Gemini 프로바이더.

- 기본 모델: `gemini-2.5-flash-lite` (가장 저렴, 한국어 OK, JSON 스키마 강제 지원).
  품질이 부족하면 `gemini-2.5-flash` 또는 `gemini-2.5-pro` 로 LLM_MODEL 만 바꾸면 됨.
- Google 의 신규 SDK(`google-genai`, `from google import genai`) 사용.
- JSON 강제는 `response_mime_type="application/json"` + `response_schema` 로 처리.
- Gemini 의 JSON Schema 는 `additionalProperties` / `$ref` / `allOf` 등을 거부하므로
  Anthropic 용으로 만든 스키마를 Gemini 가 수용하는 부분집합으로 정규화한다.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors

from .base import LLMError, LLMProvider


DEFAULT_MODEL = "gemini-2.5-flash-lite"


# Gemini 가 무시하거나 거부하는 JSON Schema 키워드
_GEMINI_UNSUPPORTED_KEYS = {"additionalProperties", "$schema", "$id"}


def _sanitize_schema(node: Any) -> Any:
    """Anthropic 용 스키마에서 Gemini 가 거부하는 키/패턴을 정규화한다.

    - `additionalProperties` / `$schema` / `$id` 제거
    - `type: ["string", "null"]` 같은 배열형 type → `type: "string", nullable: True`
    """
    if isinstance(node, dict):
        out: dict = {}
        for k, v in node.items():
            if k in _GEMINI_UNSUPPORTED_KEYS:
                continue
            if k == "type" and isinstance(v, list):
                non_null = [x for x in v if x != "null"]
                has_null = "null" in v
                if len(non_null) >= 1:
                    out["type"] = non_null[0]
                else:
                    out["type"] = "string"
                if has_null:
                    out["nullable"] = True
                continue
            out[k] = _sanitize_schema(v)
        return out
    if isinstance(node, list):
        return [_sanitize_schema(x) for x in node]
    return node


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: Optional[str] = None):
        if not api_key:
            raise LLMError("Gemini API key is required")
        self.client = genai.Client(api_key=api_key)
        self.model = model or DEFAULT_MODEL

    def complete(
        self,
        system: str,
        user: str,
        response_schema: Optional[dict] = None,
        max_tokens: int = 2000,
    ) -> Union[dict, str]:
        config_kwargs: dict = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }
        if response_schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = _sanitize_schema(response_schema)

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=user,
                config=genai_types.GenerateContentConfig(**config_kwargs),
            )
        except genai_errors.APIError as e:
            raise LLMError(f"Gemini API error: {e}") from e
        except Exception as e:  # 네트워크/타임아웃 등
            raise LLMError(f"Gemini call failed: {e}") from e

        text = (response.text or "").strip()
        if not text:
            raise LLMError(
                f"Gemini returned empty text. "
                f"finish_reason={getattr(response.candidates[0], 'finish_reason', None) if response.candidates else None}"
            )

        if response_schema is None:
            return text

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"Gemini returned non-JSON despite schema constraint: {e}. "
                f"First 200 chars: {text[:200]!r}"
            ) from e
