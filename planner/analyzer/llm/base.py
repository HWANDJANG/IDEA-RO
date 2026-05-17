"""LLM 프로바이더 추상 인터페이스.

프로바이더 구현체(claude/openai/gemini)는 모두 이 클래스를 상속해 동일한 시그니처로
`complete()` 를 노출한다. 환경변수 LLM_PROVIDER 로 골라 끼울 수 있도록 한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Union


class LLMError(Exception):
    """LLM 호출 실패 (네트워크, 인증, 응답 파싱 등 모든 실패의 단일 예외)."""


class LLMProvider(ABC):
    """프로바이더의 공통 인터페이스."""

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        response_schema: Optional[dict] = None,
        max_tokens: int = 2000,
    ) -> Union[dict, str]:
        """모델에 1회 호출 후 응답을 돌려준다.

        - `response_schema` 가 주어지면 그 JSON Schema 를 강제하고 dict 를 반환한다.
          (Anthropic Opus 4.7 의 `output_config.format` / Structured Outputs 사용)
        - `response_schema` 가 없으면 자유 텍스트 str 을 반환한다.
        - 어떤 단계에서든 실패하면 `LLMError` 를 던진다.
        """
        raise NotImplementedError
