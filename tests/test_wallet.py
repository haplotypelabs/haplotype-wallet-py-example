import time

import httpx2
import pytest
from mcp.types import CallToolResult, TextContent

from tests.conftest import FakeAuthKit
from wallet_example import wallet
from wallet_example.config import Settings
from wallet_example.oauth import TokenSet, WalletOAuth
from wallet_example.sessions import Session
from wallet_example.wallet import (
    BearerAuth,
    ReconnectRequired,
    ToolError,
    WalletErrorInfo,
    access_token,
    list_samples,
)


def session_with(expires_in: float, refresh_token: str | None = "refresh-0") -> Session:
    tokens = TokenSet("access-0", refresh_token, time.time() + expires_in, "openid samples:read")
    return Session(tokens=tokens)


async def test_fresh_token_is_used_as_is(settings: Settings, authkit: FakeAuthKit) -> None:
    session = session_with(expires_in=600)
    assert await access_token(session, WalletOAuth(settings, authkit.client())) == "access-0"
    assert authkit.token_requests == []


async def test_expiring_token_is_refreshed(settings: Settings, authkit: FakeAuthKit) -> None:
    session = session_with(expires_in=30)
    token = await access_token(session, WalletOAuth(settings, authkit.client()))
    assert token != "access-0"
    assert [r["grant_type"] for r in authkit.token_requests] == ["refresh_token"]
    assert session.tokens is not None
    assert session.tokens.refresh_token == "refresh-1"


async def test_refused_refresh_forgets_tokens(settings: Settings, authkit: FakeAuthKit) -> None:
    authkit.refresh_error = "invalid_grant"
    session = session_with(expires_in=0)
    with pytest.raises(ReconnectRequired):
        await access_token(session, WalletOAuth(settings, authkit.client()))
    assert session.tokens is None


async def test_other_refresh_errors_keep_tokens(settings: Settings, authkit: FakeAuthKit) -> None:
    authkit.refresh_error = "server_error"
    session = session_with(expires_in=0)
    with pytest.raises(Exception, match="server_error"):
        await access_token(session, WalletOAuth(settings, authkit.client()))
    assert session.tokens is not None


async def test_bearer_auth_refreshes_once_on_401(settings: Settings, authkit: FakeAuthKit) -> None:
    session = session_with(expires_in=600)
    auth = BearerAuth(session, WalletOAuth(settings, authkit.client()))
    seen: list[str] = []

    def mcp(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["Authorization"])
        ok = request.headers["Authorization"] != "Bearer access-0"
        return httpx2.Response(200 if ok else 401)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(mcp), auth=auth) as http:
        response = await http.post("https://api.example.test/mcp", content=b"{}")

    assert response.status_code == 200
    assert seen[0] == "Bearer access-0"
    assert seen[1] != seen[0]
    assert len(seen) == 2


async def test_bearer_auth_flags_reconnect(settings: Settings, authkit: FakeAuthKit) -> None:
    authkit.refresh_error = "invalid_grant"
    session = session_with(expires_in=600)
    auth = BearerAuth(session, WalletOAuth(settings, authkit.client()))
    transport = httpx2.MockTransport(lambda _: httpx2.Response(401))

    async with httpx2.AsyncClient(transport=transport, auth=auth) as http:
        with pytest.raises(ReconnectRequired):
            await http.post("https://api.example.test/mcp", content=b"{}")

    assert auth.reconnect_required
    assert session.tokens is None


async def test_list_samples_reads_structured_content(
    settings: Settings, authkit: FakeAuthKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = {"sample_id": "smp_1", "label": "Sample #1", "source_filename": "a.txt"}

    async def fake_call_tool(*_: object) -> CallToolResult:
        return CallToolResult(content=[], structured_content={"result": [sample]})

    monkeypatch.setattr(wallet, "call_tool", fake_call_tool)
    oauth = WalletOAuth(settings, authkit.client())
    assert await list_samples(settings, session_with(600), oauth) == [sample]


async def test_list_samples_tool_error(
    settings: Settings, authkit: FakeAuthKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_call_tool(*_: object) -> CallToolResult:
        text = TextContent(
            type="text", text="This app was not granted the samples:read permission."
        )
        envelope = {"code": "missing_scope", "retryable": False, "scope": "samples:read"}
        # Built from the wire format, as the MCP SDK parses a JSON-RPC result.
        return CallToolResult.model_validate(
            {
                "content": [text.model_dump()],
                "isError": True,
                "_meta": {"ai.haplotype/error": envelope},
            }
        )

    monkeypatch.setattr(wallet, "call_tool", fake_call_tool)
    oauth = WalletOAuth(settings, authkit.client())
    with pytest.raises(ToolError, match="samples:read") as caught:
        await list_samples(settings, session_with(600), oauth)
    assert caught.value.info == WalletErrorInfo(
        code="missing_scope", retryable=False, scope="samples:read"
    )
