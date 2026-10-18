"""Settings, read from environment variables. See `.env.example` for the full list."""

import os
import secrets
from dataclasses import dataclass

from wallet_example.contract import DEFAULT_ISSUER, DEFAULT_MCP_URL


@dataclass(frozen=True)
class Settings:
    issuer: str
    """AuthKit issuer. Endpoints come from `<issuer>/.well-known/openid-configuration`."""
    mcp_url: str
    """The MCP endpoint. Also the OAuth `resource`, so it decides the token's audience."""
    client_id: str
    client_secret: str
    scopes: str
    app_url: str
    port: int
    session_secret: str
    test_hooks: bool = False
    """Registers test-only routes. Never turn this on outside tests."""

    @property
    def redirect_uri(self) -> str:
        return f"{self.app_url}/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.app_url.startswith("https://")


def _env(name: str, default: str = "") -> str:
    """Read a variable, dropping one pair of surrounding quotes.

    `uv run --env-file` needs quotes around values with spaces; `docker run --env-file`
    keeps them as part of the value. Stripping them lets one .env file serve both.
    """
    value = os.environ.get(name, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value or default


def load_settings() -> Settings:
    missing = [k for k in ("WALLET_CLIENT_ID", "WALLET_CLIENT_SECRET") if not _env(k)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in.")
    return Settings(
        issuer=_env("WALLET_ISSUER", DEFAULT_ISSUER).rstrip("/"),
        mcp_url=_env("WALLET_MCP_URL", DEFAULT_MCP_URL),
        client_id=_env("WALLET_CLIENT_ID"),
        client_secret=_env("WALLET_CLIENT_SECRET"),
        scopes=_env("WALLET_SCOPES", "openid offline_access samples:read"),
        app_url=_env("APP_URL", "http://localhost:8787").rstrip("/"),
        port=int(_env("PORT", "8787")),
        # Sessions live in memory and die with the process, so a random secret per run is
        # enough. Set SESSION_SECRET once you keep sessions in a shared store.
        session_secret=_env("SESSION_SECRET") or secrets.token_hex(32),
        test_hooks=_env("EXAMPLE_TEST_HOOKS") == "1",
    )
