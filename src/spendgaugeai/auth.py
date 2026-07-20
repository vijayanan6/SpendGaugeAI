"""
auth.py — one shared secret, two schemes (docs/DESIGN.md §5).

- Bearer token for machine calls (POST /usage/log, POST /usage/credit).
- HTTP Basic (fixed username "spendgaugeai", the API key as password) for
  browser access (GET /usage, GET /usage/data, GET /static/*) — the one
  credential scheme browsers handle natively, no login page needed.

Precedence is exclusive, not a fallback pair: once SPENDGAUGEAI_API_KEY is set
via the environment, it is the *only* valid key. The persisted server_config
key is never also checked — see the module docstring rationale in
docs/DESIGN.md §5 for why "env OR persisted" would be a real leak surface.

The key must never be silently unset: if no env var is set, resolve_api_key()
generates one, persists it, and returns it so the caller (cli.py) can print it
once at startup.
"""
import os
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer

from . import database

BASIC_USERNAME = "spendgaugeai"

_active_api_key: str | None = None
_key_source: str | None = None  # "env" | "generated" | "persisted"


def resolve_api_key() -> tuple[str, str]:
    """Resolve the one active API key for this process. Call once at startup.

    Returns (api_key, source) where source is "env", "persisted", or
    "generated" — cli.py uses this to decide whether to print the key.
    """
    global _active_api_key, _key_source

    env_key = os.environ.get("SPENDGAUGEAI_API_KEY")
    if env_key:
        _active_api_key, _key_source = env_key, "env"
        return _active_api_key, _key_source

    existing = database.server_config_get()
    if existing:
        _active_api_key, _key_source = existing["api_key"], "persisted"
        return _active_api_key, _key_source

    generated = secrets.token_urlsafe(32)
    database.server_config_set_api_key(generated)
    _active_api_key, _key_source = generated, "generated"
    return _active_api_key, _key_source


def get_active_api_key() -> str:
    if _active_api_key is None:
        # Defensive — resolve_api_key() should already have run at startup
        # (cli.py / app lifespan). Resolving lazily here still keeps the same
        # exclusive-precedence rule intact if it hasn't.
        resolve_api_key()
    return _active_api_key


_bearer_scheme = HTTPBearer(auto_error=False)
_basic_scheme = HTTPBasic(auto_error=False)


async def require_bearer(request: Request, creds=Depends(_bearer_scheme)) -> None:
    active_key = get_active_api_key()
    if creds is None or not secrets.compare_digest(creds.credentials, active_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_basic(creds: HTTPBasicCredentials = Depends(_basic_scheme)) -> None:
    active_key = get_active_api_key()
    valid_user = creds is not None and secrets.compare_digest(creds.username, BASIC_USERNAME)
    valid_pass = creds is not None and secrets.compare_digest(creds.password, active_key)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="SpendGaugeAI"'},
        )


async def require_bearer_or_basic(
    bearer_creds=Depends(_bearer_scheme),
    basic_creds: HTTPBasicCredentials | None = Depends(_basic_scheme),
) -> None:
    """POST /usage/credit is called both by scripts (Bearer, same as
    /usage/log) and by the dashboard's browser JS — which only has the
    Basic-Auth credential the browser cached from loading /usage, since JS
    can never read that credential back out to set a Bearer header itself.
    Accept either scheme against the one shared key rather than inventing a
    login/session/cookie flow for the browser side."""
    active_key = get_active_api_key()
    if bearer_creds is not None and secrets.compare_digest(bearer_creds.credentials, active_key):
        return
    if (
        basic_creds is not None
        and secrets.compare_digest(basic_creds.username, BASIC_USERNAME)
        and secrets.compare_digest(basic_creds.password, active_key)
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
