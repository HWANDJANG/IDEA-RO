"""환경 변수 기반 LLM 프로바이더 선택.

LLM_PROVIDER (기본 claude) + LLM_API_KEY (필수) + LLM_MODEL (선택) 을 읽어
해당 클래스를 인스턴스화한다.
"""

from __future__ import annotations

import os
from typing import Optional

from .base import LLMProvider
from .claude import ClaudeProvider
from .gemini import GeminiProvider
from .openai import OpenAIProvider


_PROVIDERS = {
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def get_llm_provider(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> LLMProvider:
    provider = (provider or os.environ.get("LLM_PROVIDER", "claude")).strip().lower()
    api_key = api_key or os.environ.get("LLM_API_KEY", "").strip()
    model = model or os.environ.get("LLM_MODEL", "").strip() or None

    if not api_key:
        raise ValueError(
            "LLM_API_KEY 환경 변수가 비어있습니다. .env 파일에 키를 설정하세요."
        )

    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER={provider!r}. Allowed: {list(_PROVIDERS)}"
        )
    return cls(api_key=api_key, model=model)
