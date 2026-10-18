import time
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import MCP_URL, FakeAuthKit
from wallet_example.config import Settings
from wallet_example.oauth import OAuthError, TokenSet, WalletOAuth, new_pkce_pair


async def test_authorization_url(settings: Settings, authkit: FakeAuthKit) -> None:
    oauth = WalletOAuth(settings, authkit.client())
    url = urlsplit(await oauth.authorization_url("state-1", "challenge-1"))
    params = {k: v[0] for k, v in parse_qs(url.query).items()}

    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://auth.example.test/oauth2/authorize"
    assert params == {
        "response_type": "code",
        "client_id": "client_test",
        "redirect_uri": "http://localhost:8787/callback",
        "scope": "openid offline_access samples:read",
        "state": "state-1",
        "code_challenge": "challenge-1",
        "code_challenge_method": "S256",
        "resource": MCP_URL,
    }


async def test_exchange_code_sends_resource(settings: Settings, authkit: FakeAuthKit) -> None:
    oauth = WalletOAuth(settings, authkit.client())
    tokens = await oauth.exchange_code("code-1", "verifier-1")

    [form] = authkit.token_requests
    assert form == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": "http://localhost:8787/callback",
        "code_verifier": "verifier-1",
        "client_id": "client_test",
        "client_secret": "secret_test",
        "resource": MCP_URL,
    }
    assert tokens.refresh_token == "refresh-1"
    assert tokens.scope == "openid offline_access samples:read scores:read"
    assert 890 < tokens.expires_at - time.time() <= 900


async def test_scope_falls_back_to_the_access_token_claim(
    settings: Settings, authkit: FakeAuthKit
) -> None:
    authkit.include_scope = False
    tokens = await WalletOAuth(settings, authkit.client()).exchange_code("code-1", "v")
    assert tokens.scope == "openid offline_access samples:read scores:read"


async def test_refresh_sends_resource(settings: Settings, authkit: FakeAuthKit) -> None:
    oauth = WalletOAuth(settings, authkit.client())
    old = TokenSet("old-access", "refresh-0", expires_at=0, scope="openid samples:read")
    new = await oauth.refresh(old)

    [form] = authkit.token_requests
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "refresh-0"
    assert form["resource"] == MCP_URL
    assert new.access_token != "old-access"
    assert not new.expires_within(60)


async def test_refresh_refused_raises_invalid_grant(
    settings: Settings, authkit: FakeAuthKit
) -> None:
    authkit.refresh_error = "invalid_grant"
    oauth = WalletOAuth(settings, authkit.client())
    with pytest.raises(OAuthError) as caught:
        await oauth.refresh(TokenSet("a", "revoked", expires_at=0, scope=""))
    assert caught.value.error == "invalid_grant"


async def test_refresh_without_refresh_token(settings: Settings, authkit: FakeAuthKit) -> None:
    oauth = WalletOAuth(settings, authkit.client())
    with pytest.raises(OAuthError) as caught:
        await oauth.refresh(TokenSet("a", None, expires_at=0, scope="openid"))
    assert caught.value.error == "invalid_grant"
    assert authkit.token_requests == []


def test_pkce_pair() -> None:
    verifier, challenge = new_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge
    assert challenge != verifier
