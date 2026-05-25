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
        *,
        cached_content: Optional[str] = None,
    ) -> Union[dict, str]:
        """모델에 1회 호출 후 응답을 돌려준다.

        - `response_schema` 가 주어지면 그 JSON Schema 를 강제하고 dict 를 반환한다.
          (Anthropic Opus 4.7 의 `output_config.format` / Structured Outputs 사용)
        - `response_schema` 가 없으면 자유 텍스트 str 을 반환한다.
        - `cached_content` 가 주어지면 (Gemini) 캐시된 컨텍스트를 prefix 로 재사용한다.
          system 인자는 무시되고 user 만 변동 분(suffix)으로 보낸다.
        - 어떤 단계에서든 실패하면 `LLMError` 를 던진다.
        """
        raise NotImplementedError

    def create_cache(
        self,
        system: str,
        content: str,
        *,
        ttl_seconds: int = 300,
    ) -> Optional[str]:
        """정적 컨텍스트(system + content)를 캐시로 만들어 cache 이름(reference)을 반환.

        - 캐싱 미지원 프로바이더는 None 을 반환 (호출자는 일반 complete() 로 fallback).
        - 컨텐츠가 너무 작아 캐싱이 거부되면 None 반환 (예외 던지지 않음).
        - 캐시 TTL 만료 시 Gemini 가 자동 삭제하므로 명시적 cleanup 불필요.
        """
        return None

    def supports_caching(self) -> bool:
        return False

    def complete_vision(
        self,
        system: str,
        user: str,
        image_bytes: bytes,
        mime_type: str,
        response_schema: Optional[dict] = None,
        max_tokens: int = 2000,
    ) -> Union[dict, str]:
        """이미지 + 텍스트 입력. Gemini 구현 필수, 그 외는 NotImplementedError."""
        raise LLMError(
            f"{type(self).__name__} 는 이미지 입력을 지원하지 않습니다. "
            f"LLM_PROVIDER=gemini 로 설정하세요."
        )
