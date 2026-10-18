"""Read the user's data from Haplotype Wallet over MCP, refreshing tokens as needed."""

from collections.abc import AsyncGenerator
from typing import cast

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Implementation, TextContent

from wallet_example.config import Settings
from wallet_example.contract import LIST_SAMPLES_TOOL, ListSamplesResult, SampleRecord
from wallet_example.oauth import OAuthError, WalletOAuth
from wallet_example.sessions import Session

# Refresh a little before the access token expires (it lives 15 minutes).
REFRESH_MARGIN_SECONDS = 60


class ReconnectRequired(Exception):
    """The refresh token stopped working, usually because the user disconnected this app
    in their wallet. The app has forgotten the tokens and must ask the user to connect again.
    """


class WalletError(Exception):
    """The wallet answered, but with an error the user should see."""


async def access_token(session: Session, oauth: WalletOAuth, *, force_refresh: bool = False) -> str:
    """Return a usable access token for this session, refreshing it when it is about to expire.

    Raises `ReconnectRequired` when the refresh is refused with `invalid_grant`.
    """
    async with session.lock:
        tokens = session.tokens
        if tokens is None:
            raise ReconnectRequired
        if force_refresh or tokens.expires_within(REFRESH_MARGIN_SECONDS):
            try:
                session.tokens = await oauth.refresh(tokens)
            except OAuthError as e:
                if e.error != "invalid_grant":
                    raise
                session.tokens = None
                raise ReconnectRequired from e
        assert session.tokens is not None
        return session.tokens.access_token


class BearerAuth(httpx2.Auth):
    """Puts the access token on each MCP request. On a 401, refreshes once and retries."""

    def __init__(self, session: Session, oauth: WalletOAuth) -> None:
        self._session = session
        self._oauth = oauth
        self.reconnect_required = False
        """Set when a refresh failed inside a request. The MCP SDK wraps errors raised
        here, so the caller checks this flag instead of the exception type."""

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {await self._token(False)}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {await self._token(True)}"
            yield request

    async def _token(self, force_refresh: bool) -> str:
        try:
            return await access_token(self._session, self._oauth, force_refresh=force_refresh)
        except ReconnectRequired:
            self.reconnect_required = True
            raise


async def call_tool(
    settings: Settings, session: Session, oauth: WalletOAuth, name: str
) -> CallToolResult:
    """Call one MCP tool as the connected user.

    The MCP endpoint is stateless: each call can open a fresh connection, no session ID to keep.
    """
    # Refresh up front when needed, so the common case raises ReconnectRequired cleanly.
    try:
        await access_token(session, oauth)
    except (OAuthError, httpx2.HTTPError) as e:
        raise WalletError(f"Could not refresh the access token ({e}).") from e
    auth = BearerAuth(session, oauth)
    try:
        async with (
            httpx2.AsyncClient(auth=auth, timeout=httpx2.Timeout(30)) as http,
            Client(
                streamable_http_client(settings.mcp_url, http_client=http),
                client_info=Implementation(name="haplotype-wallet-py-example", version="0.1.0"),
            ) as client,
        ):
            return await client.call_tool(name, {})
    except Exception as e:
        if auth.reconnect_required:
            raise ReconnectRequired from e
        raise WalletError("Could not reach Haplotype Wallet. Try again in a moment.") from e


async def list_samples(
    settings: Settings, session: Session, oauth: WalletOAuth
) -> list[SampleRecord]:
    result = await call_tool(settings, session, oauth, LIST_SAMPLES_TOOL)
    if result.is_error:
        text = " ".join(c.text for c in result.content if isinstance(c, TextContent))
        raise WalletError(text or "Haplotype Wallet returned an error.")
    # A tool that returns a list wraps it as {"result": [...]} in `structuredContent`.
    structured = cast(ListSamplesResult, result.structured_content or {})
    return structured.get("result", [])
