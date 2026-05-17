"""OpenAI 프로바이더 — 추후 구현 예정.

LLMProvider 인터페이스를 그대로 따르면 됨. 환경변수 LLM_PROVIDER=openai 로 선택.
"""

from __future__ import annotations

from typing import Optional, Union

from .base import LLMError, LLMProvider


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: Optional[str] = None):
        # TODO: pip install openai 후 동일 인터페이스로 구현
        raise NotImplementedError(
            "OpenAIProvider is not implemented yet. "
            "Set LLM_PROVIDER=claude or implement this class."
        )

    def complete(
        self,
        system: str,
        user: str,
        response_schema: Optional[dict] = None,
        max_tokens: int = 2000,
    ) -> Union[dict, str]:
        raise NotImplementedError
