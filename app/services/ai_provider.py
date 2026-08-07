"""Provider-agnostic AI completion layer for the /admin/ai-settings page.

Every provider adapter below (Anthropic / OpenAI / OpenAI-compatible)
implements the same `.complete()` shape — one system prompt, one user
turn, no history, no tool use. This matches what `generate_pms_notes`
(Tab 5 AI Notes) needs and is what `test_provider()` uses to verify a
key before it's ever saved or activated.

Which provider is "active" is resolved from the ai_provider_configs
table (admin-managed via /api/ai-settings) — see
`resolve_active_provider()`. If no row is active (including before
that table has ever been touched), this falls back to the .env
ANTHROPIC_API_KEY / ANTHROPIC_MODEL, so behavior is unchanged unless
an admin explicitly opts in.

The PMS-Agent chat (pms_agent_service.py) is a separate, Anthropic-
specific multi-turn call site (conversation history + prompt caching)
that predates this abstraction and isn't restructured to go through
`.complete()` here — re-engineering its JSON-extraction prompt for
OpenAI-shaped multi-turn calls is out of scope. It still benefits from
the admin settings page through `resolve_anthropic_credentials()`
below: if the active provider is Anthropic, the chat uses that row's
key/model; otherwise (a non-Anthropic provider is active, or none is)
it falls back to today's .env values, unchanged.
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


class AIProvider(ABC):
    @abstractmethod
    def complete(
        self,
        *,
        system_prompt: str,
        user_text: str,
        max_tokens: int,
    ) -> AICompletion:
        """Single-turn completion: one system prompt, one user turn."""


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

    def complete(self, *, system_prompt, user_text, max_tokens):
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
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as e:  # noqa: BLE001
            raise AIProviderError(str(e)) from e
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        return AICompletion(text=text, stop_reason=getattr(resp, "stop_reason", None))


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

    def complete(self, *, system_prompt, user_text, max_tokens):
        client = self._client_()
        try:
            resp = client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
            )
        except Exception as e:  # noqa: BLE001
            raise AIProviderError(str(e)) from e
        choice = resp.choices[0]
        return AICompletion(
            text=choice.message.content or "",
            stop_reason=getattr(choice, "finish_reason", None),
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
    """The entry point single-turn call sites (AI Notes) use. Returns
    None when no provider is usable at all."""
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


def resolve_anthropic_credentials() -> Optional[tuple[str, str]]:
    """For the PMS-Agent chat's Anthropic-specific multi-turn call site
    (pms_agent_service.py), which isn't restructured to go through
    `.complete()` above. Returns (api_key, model) from the active
    provider ONLY if it's an Anthropic-type row; otherwise (a non-
    Anthropic provider is active, or none is, or the row is
    undecryptable) falls back to the .env ANTHROPIC_API_KEY /
    ANTHROPIC_MODEL, unchanged from before this module existed.
    Returns None only when neither source has a usable key."""
    row = _fetch_active_config_row()
    if row is not None and row.get("provider") == "anthropic":
        try:
            api_key = decrypt_secret(row["api_key_encrypted"])
            return api_key, (row.get("model") or DEFAULT_ANTHROPIC_MODEL)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ai_provider: active anthropic config id=%s is unusable, falling back to .env: %s",
                row.get("id"), e,
            )
    if settings.anthropic_api_key:
        return settings.anthropic_api_key, settings.anthropic_model
    return None
