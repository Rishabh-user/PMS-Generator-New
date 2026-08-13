"""Admin-managed AI provider configuration (Anthropic / OpenAI / OpenAI-
compatible). See app.services.ai_provider for the completion layer this
feeds; ai_provider.resolve_active_provider() is what BOTH the AI Notes
and PMS-Agent chat features read at request time — any saved provider
type can power either feature.

No backend auth — matches this project's existing /api/admin/* convention
(see app/routes/admin_routes.py): the page is meant to sit behind a trusted
network / reverse proxy. Since this endpoint additionally accepts and
stores API keys, treat that boundary as load-bearing.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.services import ai_provider, ai_provider_store
from app.services.crypto import CredentialCryptoError, decrypt_secret, encrypt_secret
from app.services.session_store import SessionStoreUnavailableError


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai-settings", tags=["ai-settings"])

AIProviderLiteral = Literal["anthropic", "openai", "openai_compatible"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AIProviderConfigOut(BaseModel):
    id: int
    provider: AIProviderLiteral
    label: str
    model: Optional[str]
    base_url: Optional[str]
    # Last few characters of the real (decrypted) key, e.g. "••••ab12" —
    # never the full key or the ciphertext.
    api_key_hint: str
    is_active: bool
    created_at: str
    updated_at: str


class AISettingsStateOut(BaseModel):
    configs: list[AIProviderConfigOut]
    active_id: Optional[int]
    # Whether the chat / notes features would still work if no row is
    # active, via .env's ANTHROPIC_API_KEY — surfaced so the settings
    # page can explain why the app keeps working with zero saved rows.
    env_fallback_available: bool


class AIProviderConfigCreate(BaseModel):
    provider: AIProviderLiteral
    label: str = Field(min_length=1, max_length=120)
    api_key: str = Field(min_length=1)
    model: Optional[str] = None
    base_url: Optional[str] = None
    # Activate immediately on successful creation (deactivating any
    # other active row). Defaults on since the common flow is "add a
    # provider I actually want to switch to right now".
    activate: bool = True


class AIProviderConfigUpdate(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=120)
    # Omit to keep the existing key.
    api_key: Optional[str] = Field(default=None, min_length=1)
    model: Optional[str] = None
    base_url: Optional[str] = None
    # Distinguishes "field omitted from the request" (leave alone) from
    # "field present but null" (clear it) for model/base_url, since
    # pydantic can't tell those apart from the attribute value alone.
    model_set: bool = False
    base_url_set: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Database unavailable: {detail}")


def _hint(decrypted_key: str) -> str:
    tail = decrypted_key[-4:] if len(decrypted_key) >= 4 else decrypted_key
    return f"••••{tail}"


def _iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _to_out(row: dict) -> AIProviderConfigOut:
    try:
        hint = _hint(decrypt_secret(row["api_key_encrypted"]))
    except CredentialCryptoError:
        hint = "••••????"
    return AIProviderConfigOut(
        id=row["id"],
        provider=row["provider"],
        label=row["label"],
        model=row.get("model"),
        base_url=row.get("base_url"),
        api_key_hint=hint,
        is_active=row["is_active"],
        created_at=_iso(row.get("created_at")),
        updated_at=_iso(row.get("updated_at")),
    )


def _test_or_400(provider_obj: ai_provider.AIProvider) -> None:
    """Runs a real, minimal completion through the given provider so a
    bad key/model/base_url is rejected before it's ever saved or
    activated — this is the guarantee behind "the system keeps working
    as it is"."""
    try:
        ai_provider.test_provider(provider_obj)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not verify this provider: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_model=AISettingsStateOut)
def list_ai_settings():
    try:
        rows = ai_provider_store.list_configs()
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))
    active = next((r for r in rows if r["is_active"]), None)
    return AISettingsStateOut(
        configs=[_to_out(r) for r in rows],
        active_id=active["id"] if active else None,
        env_fallback_available=bool(settings.anthropic_api_key),
    )


@router.post("", response_model=AIProviderConfigOut)
def create_ai_setting(payload: AIProviderConfigCreate):
    if payload.provider == "openai_compatible" and not payload.base_url:
        raise HTTPException(
            status_code=400, detail="base_url is required for an OpenAI-compatible provider"
        )

    provider_obj = ai_provider.build_provider(
        payload.provider, payload.api_key, payload.model, payload.base_url
    )
    _test_or_400(provider_obj)

    try:
        encrypted = encrypt_secret(payload.api_key)
    except CredentialCryptoError as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        row = ai_provider_store.create_config(
            provider=payload.provider,
            label=payload.label,
            api_key_encrypted=encrypted,
            model=payload.model,
            base_url=payload.base_url,
            activate=payload.activate,
        )
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))

    logger.info(
        "ai_provider_config.create id=%s provider=%s label=%r activated=%s",
        row["id"], row["provider"], row["label"], payload.activate,
    )
    return _to_out(row)


@router.patch("/{config_id}", response_model=AIProviderConfigOut)
def update_ai_setting(config_id: int, payload: AIProviderConfigUpdate):
    try:
        row = ai_provider_store.get_config(config_id)
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider config not found")

    new_model = payload.model if payload.model_set else row.get("model")
    new_base_url = payload.base_url if payload.base_url_set else row.get("base_url")

    if row["provider"] == "openai_compatible" and not new_base_url:
        raise HTTPException(
            status_code=400, detail="base_url is required for an OpenAI-compatible provider"
        )

    # Re-verify whenever anything that affects the actual call changes.
    new_key_encrypted: Optional[str] = None
    if payload.api_key is not None or payload.model_set or payload.base_url_set:
        try:
            new_key = payload.api_key if payload.api_key is not None else decrypt_secret(row["api_key_encrypted"])
        except CredentialCryptoError as e:
            raise HTTPException(status_code=500, detail=str(e))
        provider_obj = ai_provider.build_provider(row["provider"], new_key, new_model, new_base_url)
        _test_or_400(provider_obj)
        if payload.api_key is not None:
            try:
                new_key_encrypted = encrypt_secret(payload.api_key)
            except CredentialCryptoError as e:
                raise HTTPException(status_code=500, detail=str(e))

    try:
        row = ai_provider_store.update_config(
            config_id,
            label=payload.label,
            api_key_encrypted=new_key_encrypted,
            model=new_model,
            base_url=new_base_url,
            model_set=payload.model_set,
            base_url_set=payload.base_url_set,
        )
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))

    logger.info(
        "ai_provider_config.update id=%s provider=%s label=%r key_rotated=%s",
        config_id, row["provider"], row["label"], payload.api_key is not None,
    )
    return _to_out(row)  # type: ignore[arg-type]


@router.post("/{config_id}/activate", response_model=AIProviderConfigOut)
def activate_ai_setting(config_id: int):
    try:
        row = ai_provider_store.get_config(config_id)
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider config not found")

    try:
        api_key = decrypt_secret(row["api_key_encrypted"])
    except CredentialCryptoError as e:
        raise HTTPException(status_code=500, detail=str(e))
    provider_obj = ai_provider.build_provider(row["provider"], api_key, row.get("model"), row.get("base_url"))
    _test_or_400(provider_obj)

    try:
        row = ai_provider_store.activate_config(config_id)
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))

    logger.info("ai_provider_config.activate id=%s provider=%s label=%r", config_id, row["provider"], row["label"])
    return _to_out(row)  # type: ignore[arg-type]


@router.delete("/{config_id}", status_code=204)
def delete_ai_setting(config_id: int):
    try:
        row = ai_provider_store.get_config(config_id)
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider config not found")
    if row["is_active"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the active provider — activate a different one first.",
        )
    try:
        ai_provider_store.delete_config(config_id)
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e))
    logger.info("ai_provider_config.delete id=%s provider=%s label=%r", config_id, row["provider"], row["label"])
