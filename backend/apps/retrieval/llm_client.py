from __future__ import annotations

import logging
from typing import Any, Dict, Sequence

from django.conf import settings

from backend.apps.pastpaper.parsers.llmparser import JSONChatClient, ProviderTransportConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_API_KEY_ENV = "OPENROUTER_APIKEY"


class LLMClientError(RuntimeError):
    """Base exception for LLM client failures."""


class LLMConfigurationError(LLMClientError):
    """Raised when required configuration is missing."""


class LLMResponseError(LLMClientError):
    """Raised when the upstream LLM call fails or returns invalid payloads."""


class LLMClient:
    """Async JSON-only chat client aligned with the PastPaper parser implementation."""

    def __init__(
        self,
        *,
        model: str | None = None,
        provider_cfg: ProviderTransportConfig | None = None,
        temperature: float | None = None,
    ):
        self.model = model or getattr(settings, "RETRIEVAL_LLM_MODEL", "")
        if not self.model:
            raise LLMConfigurationError("RETRIEVAL_LLM_MODEL is not configured.")
        self.temperature = (
            temperature if temperature is not None else float(getattr(settings, "RETRIEVAL_LLM_TEMPERATURE", 0.0))
        )
        self.provider_cfg = provider_cfg or self._build_provider_config()
        self._client = JSONChatClient(self.provider_cfg)

    async def complete_json(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload = await self._client.chat_json(
            model=model or self.model,
            messages=list(messages),
            temperature=self.temperature if temperature is None else temperature,
        )
        data = payload.get("data")
        if data is None:
            detail = payload.get("error") or "unknown_json_error"
            logger.warning("LLM response missing JSON payload: %s", detail)
            raise LLMResponseError(f"LLM response missing JSON payload: {detail}")
        return data

    def _build_provider_config(self) -> ProviderTransportConfig:
        base_url = getattr(settings, "RETRIEVAL_LLM_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
        timeout = float(getattr(settings, "RETRIEVAL_LLM_TIMEOUT", 45.0))
        retries = int(getattr(settings, "RETRIEVAL_LLM_MAX_RETRIES", 3))
        backoff = float(getattr(settings, "RETRIEVAL_LLM_BACKOFF_SECONDS", 0.5))
        concurrency = int(getattr(settings, "RETRIEVAL_LLM_CONCURRENCY", 8))
        api_key_env = getattr(settings, "RETRIEVAL_LLM_API_KEY_ENV", DEFAULT_API_KEY_ENV) or DEFAULT_API_KEY_ENV
        return ProviderTransportConfig(
            base_url=base_url,
            api_key_env=api_key_env,
            timeout_sec=timeout,
            max_retries=retries,
            initial_backoff_sec=backoff,
            concurrency=concurrency,
            force_json=True,
            default_temperature=self.temperature,
            model_ids=[self.model],
        )


__all__ = [
    "LLMClient",
    "LLMClientError",
    "LLMConfigurationError",
    "LLMResponseError",
]
