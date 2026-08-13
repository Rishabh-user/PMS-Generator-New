"""Provider-agnostic AI completion layer for the /admin/ai-settings page.

Every provider adapter below (Anthropic / OpenAI / OpenAI-compatible)
implements the same `.complete_chat()` primitive — one system prompt,
a list of {role, content} turns, no tool use. `.complete()` is a thin
single-turn convenience wrapper over it. This one shape now powers
BOTH AI call sites in this app:
  • generate_pms_notes (Tab 5 AI Notes) — single-turn, via `.complete()`.
  • pms_agent_service._extract_filters_with_ai (PMS-Agent chat slot
    extraction) — multi-turn, via `.complete_chat()`, since the chat
    passes prior conversation turns.
Any saved provider — Anthropic, OpenAI, or an OpenAI-compatible engine
(OpenRouter, DashScope, a self-hosted vLLM server, etc.) — can power
either feature; there's nothing Anthropic-specific left at the call
sites.

Which provider is "active" is resolved from the ai_provider_configs
table (admin-managed via /admin/ai-settings) — see
`resolve_active_provider()`. If no row is active (including before
that table has ever been touched), this falls back to the .env
ANTHROPIC_API_KEY / ANTHROPIC_MODEL so the app still works out of the
box; once an admin activates any provider from the settings page, that
row is used instead and .env is no longer consulted.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.services.crypto import decrypt_secret

log = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OPENAI_MODEL = "gpt-4o"


class AIProviderError(RuntimeError):
    """Raised when an AIProvider can't be constructed (missing SDK,
    bad credentials) or its completion call fails."""


@dataclass
class AICompletion:
    text: str
    stop_reason: Optional[str]
    # Usage isn't available from every provider/SDK in the same shape;
    # left None when the adapter can't determine it. Consumed by
    # pms_agent_service's per-call cost/perf logging (pms_agent_queries).
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


class AIProvider(ABC):
    provider: str
    model: str

    @abstractmethod
    def complete_chat(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
    ) -> AICompletion:
        """Multi-turn completion: one system prompt + a list of
        {role: 'user'|'assistant', content: str} turns."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_text: str,
        max_tokens: int,
    ) -> AICompletion:
        """Single-turn convenience wrapper over `complete_chat`."""
        return self.complete_chat(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_text}],
            max_tokens=max_tokens,
        )


class AnthropicProvider(AIProvider):
    def __init__(self, api_key: str, model: str):
        self.provider = "anthropic"
        self.model = model
        self._api_key = api_key
        self._client = None

    def _client_(self):
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as e:
                raise AIProviderError(f"anthropic SDK not installed: {e}") from e
            self._client = Anthropic(api_key=self._api_key)
        return self._client

    def complete_chat(self, *, system_prompt, messages, max_tokens):
        client = self._client_()
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001
            raise AIProviderError(str(e)) from e
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        usage = getattr(resp, "usage", None)
        return AICompletion(
            text=text,
            stop_reason=getattr(resp, "stop_reason", None),
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
        )


class OpenAIProvider(AIProvider):
    """Real OpenAI (base_url=None) AND any OpenAI-compatible endpoint
    (base_url set — e.g. OpenRouter, DashScope, a self-hosted vLLM
    server) — same adapter, since the wire format is identical."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        self.provider = "openai_compatible" if base_url else "openai"
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self._client = None

    def _client_(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise AIProviderError(f"openai SDK not installed: {e}") from e
            self._client = OpenAI(api_key=self._api_key, base_url=self.base_url or None)
        return self._client

    def complete_chat(self, *, system_prompt, messages, max_tokens):
        client = self._client_()
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        try:
            resp = client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                messages=full_messages,
            )
        except Exception as e:  # noqa: BLE001
            raise AIProviderError(str(e)) from e
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return AICompletion(
            text=choice.message.content or "",
            stop_reason=getattr(choice, "finish_reason", None),
            tokens_in=getattr(usage, "prompt_tokens", None) if usage else None,
            tokens_out=getattr(usage, "completion_tokens", None) if usage else None,
        )


def build_provider(
    provider: str, api_key: str, model: Optional[str], base_url: Optional[str]
) -> AIProvider:
    if provider == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model or DEFAULT_ANTHROPIC_MODEL)
    if provider in ("openai", "openai_compatible"):
        return OpenAIProvider(api_key=api_key, model=model or DEFAULT_OPENAI_MODEL, base_url=base_url)
    raise AIProviderError(f"Unknown AI provider: {provider!r}")


def test_provider(provider: AIProvider) -> None:
    """Raises AIProviderError if `provider` can't complete a trivial
    request. Used by the settings API to reject a bad key/model/
    base_url before it's ever saved or activated."""
    provider.complete(
        system_prompt="Reply with exactly one word.",
        user_text="Reply with the single word: OK",
        max_tokens=16,
    )


# ---------------------------------------------------------------------------
# Active-config resolution
# ---------------------------------------------------------------------------

def _fetch_active_config_row() -> Optional[dict]:
    """Returns the active ai_provider_configs row as a dict, or None if
    there isn't one — including if the table doesn't exist yet or the
    DB is unreachable. Must never raise: every call site falls back to
    .env."""
    try:
        from app.services import ai_provider_store
        for row in ai_provider_store.list_configs():
            if row.get("is_active"):
                return row
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("ai_provider: could not read ai_provider_configs, falling back to .env: %s", e)
        return None


def resolve_active_provider() -> Optional[AIProvider]:
    """The single entry point every AI call site uses — AI Notes
    (single-turn, via `.complete()`) and the PMS-Agent chat (multi-turn,
    via `.complete_chat()`) alike. Returns None when no provider is
    usable at all.

    Resolution order:
      1. The active row in ai_provider_configs (any provider type —
         Anthropic, OpenAI, or OpenAI-compatible), admin-managed at
         /admin/ai-settings. Once any row is active, .env is no longer
         consulted.
      2. The .env ANTHROPIC_API_KEY / ANTHROPIC_MODEL fallback — only
         reached when no row is active (or the DB/active row is
         unusable), so the app still works before the settings page
         has ever been touched.
    """
    row = _fetch_active_config_row()
    if row is not None:
        try:
            api_key = decrypt_secret(row["api_key_encrypted"])
            return build_provider(row["provider"], api_key, row.get("model"), row.get("base_url"))
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ai_provider: active config id=%s is unusable, falling back to .env: %s",
                row.get("id"), e,
            )
    if settings.anthropic_api_key:
        return AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    return None
