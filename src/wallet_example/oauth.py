"""OAuth 2.0 authorization code flow against Haplotype Wallet.

Haplotype Wallet's authorization server is WorkOS AuthKit. The flow is three plain HTTP
requests, written out here instead of hidden in a library so you can port them:

1. Send the browser to the authorize endpoint (`authorization_url`).
2. Exchange the returned code for tokens at the token endpoint (`exchange_code`).
3. Refresh the access token before it expires; it lives 15 minutes (`refresh`).

THE TRAP: send `resource=<WALLET_MCP_URL>` on the token request AND on every refresh,
not only on the authorize request. AuthKit sets the access token's audience (`aud`) from
`resource` on the token request. Without it, `aud` is some other client ID, and the MCP
endpoint rejects the token with 401 `invalid_token`.

The app is a confidential client (it authenticates with its client secret) and also uses
PKCE. AuthKit checks the PKCE verifier on the code exchange, so send both.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx2

from wallet_example.config import Settings


class OAuthError(Exception):
    """An OAuth error response, such as `invalid_grant` on a revoked refresh token."""

    def __init__(self, error: str, description: str | None = None) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float
    """Unix time when the access token expires."""
    scope: str
    """Space-separated scopes the user granted. Can differ from what the app asked for."""

    def expires_within(self, seconds: float) -> bool:
        return time.time() + seconds >= self.expires_at


def new_pkce_pair() -> tuple[str, str]:
    """Return a PKCE `(code_verifier, code_challenge)` pair, S256 method."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class WalletOAuth:
    def __init__(self, settings: Settings, http: httpx2.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._metadata: dict[str, Any] | None = None

    async def _endpoint(self, name: str) -> str:
        """Read an endpoint URL from OIDC discovery, fetched once and cached."""
        metadata = self._metadata
        if metadata is None:
            url = f"{self._settings.issuer}/.well-known/openid-configuration"
            response = await self._http.get(url)
            response.raise_for_status()
            metadata = self._metadata = dict[str, Any](response.json())
        return str(metadata[name])

    async def authorization_url(self, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_uri,
            "scope": self._settings.scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Harmless here, and required on the token request below.
            "resource": self._settings.mcp_url,
        }
        return f"{await self._endpoint('authorization_endpoint')}?{urlencode(params)}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri,
                "code_verifier": code_verifier,
            },
            previous=None,
        )

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        """Trade the refresh token for a new access token.

        Raises `OAuthError("invalid_grant")` once the user has disconnected this app in
        their wallet. The app must then ask the user to connect again.
        """
        if not tokens.refresh_token:
            # No refresh token means `offline_access` was not requested or not granted.
            raise OAuthError("invalid_grant", "no refresh token")
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token},
            previous=tokens,
        )

    async def _token_request(self, form: dict[str, str], previous: TokenSet | None) -> TokenSet:
        form = {
            **form,
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
            # THE TRAP: without `resource` here, the access token's audience is wrong
            # and the MCP endpoint answers 401. It must be on every token request,
            # refreshes included.
            "resource": self._settings.mcp_url,
        }
        response = await self._http.post(await self._endpoint("token_endpoint"), data=form)
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or "access_token" not in body:
            raise OAuthError(
                str(body.get("error", f"http_{response.status_code}")),
                body.get("error_description"),
            )
        return TokenSet(
            access_token=body["access_token"],
            # A refresh response may leave out fields that did not change.
            refresh_token=body.get("refresh_token") or (previous and previous.refresh_token),
            expires_at=time.time() + float(body.get("expires_in", 300)),
            scope=body.get("scope") or _scope_claim(body["access_token"]),
        )


def _scope_claim(access_token: str) -> str:
    """Read the `scope` claim from an access token.

    Token responses do not always include `scope`; the access token always carries it.
    The token came straight from the token endpoint over TLS, so reading the payload
    without verifying the signature is fine here. Verifying tokens is the MCP server's job.
    """
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(claims.get("scope", ""))
    except (IndexError, ValueError):
        return ""
