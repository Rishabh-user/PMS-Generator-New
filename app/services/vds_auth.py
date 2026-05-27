"""Cross-service authentication — verify the VDS user-management
backend's JWT locally, then enrich with role / name by calling the VDS
`/api/auth/me` endpoint. Cached per-token for 60 seconds to keep the
hot path fast.

Setup:
  • PMS backend must share the VDS backend's `SECRET_KEY` env var so the
    HS256 verification matches what VDS issues.
  • PMS backend must know how to reach VDS — set `VDS_BACKEND_URL`
    (e.g. https://spe-valvesheet-backend-staging.onrender.com or
    http://localhost:8000 in dev). The /api/auth/me call uses the same
    Bearer token the frontend already has in localStorage, so no
    separate service-account credentials are needed.

The FastAPI dependency `get_current_user` returns a dict with at least:
    {
        "user_id":   str,
        "email":     str,
        "role_code": str | None,   # MAKER | CHECKER | APPROVER | ADMIN | ...
        "full_name": str,
    }
"""
from __future__ import annotations

import os
import time
import logging
from typing import Optional

import jwt
import httpx
from fastapi import Depends, Header, HTTPException, status


logger = logging.getLogger(__name__)


# Settings come from environment so the same code runs locally + on Render
# without code changes.
_SECRET_KEY = os.getenv("SECRET_KEY", "spe-valvesheet-dev-secret")
_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
_VDS_URL = (os.getenv("VDS_BACKEND_URL") or "http://localhost:8000").rstrip("/")
_HTTP_TIMEOUT = 10.0


# Token → user-record cache. Keyed by the JWT string. Entries expire
# after _CACHE_TTL_S seconds — short enough that role / name changes on
# the VDS side propagate quickly, long enough that bursts of API calls
# don't hammer VDS.
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, dict]] = {}


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _verify_jwt_local(token: str) -> dict:
    """Verify signature + expiry against the shared VDS SECRET_KEY."""
    try:
        return jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _fetch_vds_user(token: str) -> dict:
    """Ask the VDS backend for the full user record. Falls back to a
    minimal record (no role/name) if VDS is unreachable so the workflow
    still functions in degraded mode."""
    try:
        resp = httpx.get(
            f"{_VDS_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("VDS /api/auth/me unreachable: %s", exc)
        return {}
    if resp.status_code == 401:
        # Bad token — VDS rejected it even though local verification
        # passed. That can happen if VDS rotated its secret. Treat as
        # auth failure to be safe.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="VDS backend rejected the token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not resp.is_success:
        logger.warning("VDS /api/auth/me returned %s: %s", resp.status_code, resp.text[:200])
        return {}
    try:
        return resp.json() or {}
    except Exception:
        return {}


def get_current_user(authorization: Optional[str] = Header(default=None, alias="Authorization")) -> dict:
    """FastAPI dependency. Returns a normalized user dict."""
    token = _extract_bearer(authorization)

    # Cache hit (still valid)
    entry = _cache.get(token)
    now = time.monotonic()
    if entry and entry[0] > now:
        return entry[1]

    # Local JWT verification — fast path, no network
    payload = _verify_jwt_local(token)
    user_id = payload.get("sub")
    email = payload.get("email")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing 'sub' claim")

    # Enrich with role/name from VDS. Tolerates VDS being unreachable.
    vds_record = _fetch_vds_user(token)
    user = {
        "user_id": vds_record.get("user_id") or user_id,
        "email": vds_record.get("email") or email or "",
        "role_code": (vds_record.get("role_code") or "").upper() or None,
        "full_name": (
            vds_record.get("full_name")
            or " ".join(filter(None, [
                vds_record.get("first_name"),
                vds_record.get("last_name"),
            ]))
            or email
            or user_id
        ),
        "access_level": vds_record.get("access_level"),
    }

    _cache[token] = (now + _CACHE_TTL_S, user)
    # Light-weight cache eviction — drop expired entries opportunistically
    if len(_cache) > 256:
        expired = [k for k, (exp, _) in _cache.items() if exp <= now]
        for k in expired:
            _cache.pop(k, None)
    return user


def require_role(*allowed_roles: str):
    """FastAPI dependency factory — gate a route to specific roles."""
    allowed = {r.strip().upper() for r in allowed_roles if r and r.strip()}

    def _guard(user: dict = Depends(get_current_user)) -> dict:
        role = (user.get("role_code") or "").upper()
        if allowed and role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {role or '(none)'} not allowed",
            )
        return user
    return _guard
