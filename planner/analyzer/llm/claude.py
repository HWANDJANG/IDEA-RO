"""Anthropic Claude 프로바이더.

- 모델 기본값은 `claude-opus-4-7` (가장 강력한 최신 GA 모델). 환경변수 LLM_MODEL 로
  교체 가능 (예: 비용 절감용 `claude-sonnet-4-6` / `claude-haiku-4-5`).
- 같은 시스템 프롬프트 + 스키마가 매 호출 동일하게 사용되므로 prompt caching 활성화.
- JSON 강제는 `output_config.format`(Structured Outputs)으로 처리. 모델 응답이
  스키마에 맞게 자동 검증되므로 별도의 응답 후처리 재시도가 불필요.
"""

from __future__ import annotations

import json
from typing import Optional, Union

import anthropic

from .base import LLMError, LLMProvider


DEFAULT_MODEL = "claude-opus-4-7"


class ClaudeProvider(LLMProvider):
    def __init__(self, api_key: str, model: Optional[str] = None):
        if not api_key:
            raise LLMError("Claude API key is required")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or DEFAULT_MODEL

    def complete(
        self,
        system: str,
        user: str,
        response_schema: Optional[dict] = None,
        max_tokens: int = 2000,
    ) -> Union[dict, str]:
        # 시스템 프롬프트는 매번 동일 → 캐싱
        system_blocks = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user}],
        }

        if response_schema is not None:
            kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": response_schema,
                },
            }

        try:
            response = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError(f"Claude API auth failed: {e}") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Claude API rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API connection failed: {e}") from e

        # 응답에서 첫 텍스트 블록 추출
        text_block = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            None,
        )
        if text_block is None:
            raise LLMError(
                f"Claude response had no text block. stop_reason={response.stop_reason}"
            )

        if response_schema is None:
            return text_block

        # output_config.format 사용 시 응답은 검증된 JSON 문자열
        try:
            return json.loads(text_block)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"Claude returned non-JSON despite schema constraint: {e}. "
                f"First 200 chars: {text_block[:200]!r}"
            ) from e
