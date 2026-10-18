from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.conftest import MCP_URL, FakeAuthKit
from wallet_example import app as app_module
from wallet_example.app import create_app
from wallet_example.config import Settings
from wallet_example.contract import SampleRecord
from wallet_example.oauth import WalletOAuth
from wallet_example.sessions import COOKIE_NAME, Session, SessionStore
from wallet_example.wallet import ReconnectRequired, WalletError

SAMPLE: SampleRecord = {
    "sample_id": "smp_1",
    "label": "Sample #1",
    "source_filename": "genome_v5.txt",
    "file_size": 1234,
    "processed_at": "2026-10-01T12:00:00+00:00",
    "provenance": None,
}


@pytest.fixture
def client(settings: Settings, authkit: FakeAuthKit) -> TestClient:
    return TestClient(create_app(settings, authkit.client()), follow_redirects=False)


def stub_samples(monkeypatch: pytest.MonkeyPatch, result: list[SampleRecord] | Exception) -> None:
    async def fake(
        _settings: Settings, _session: Session, _oauth: WalletOAuth
    ) -> list[SampleRecord]:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app_module, "list_samples", fake)


def connect(client: TestClient) -> None:
    """Run /connect and /callback as if the user approved."""
    location = client.get("/connect").headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    response = client.get("/callback", params={"code": "code-1", "state": state})
    assert response.headers["location"] == "/"


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert (response.status_code, response.text) == (200, "ok")


def test_home_shows_connect_button(client: TestClient) -> None:
    assert 'data-testid="connect-button"' in client.get("/").text


def test_connect_redirects_to_authorize(client: TestClient) -> None:
    response = client.get("/connect")
    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["resource"] == [MCP_URL]
    assert params["code_challenge_method"] == ["S256"]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()


def test_full_flow_shows_scopes_and_samples(
    client: TestClient, authkit: FakeAuthKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_samples(monkeypatch, [SAMPLE])
    connect(client)

    [exchange] = authkit.token_requests
    assert exchange["resource"] == MCP_URL
    assert exchange["code_verifier"]

    page = client.get("/").text
    assert 'data-scope="samples:read"' in page
    assert 'data-scope="scores:read"' in page
    assert 'data-scope="openid"' not in page
    assert 'data-scope="offline_access"' not in page
    assert page.count('data-testid="sample-row"') == 1
    assert "genome_v5.txt" in page
    assert "01 Oct 2026" in page
    assert 'data-testid="disconnect-button"' in page


def test_tokens_never_reach_the_browser(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_samples(monkeypatch, [])
    connect(client)
    page = client.get("/").text
    assert "refresh-1" not in page
    assert "eyJ" not in page  # no JWT
    cookie = client.cookies[COOKIE_NAME]
    assert "refresh" not in cookie
    assert "eyJ" not in cookie


def test_no_samples_shows_empty_state(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_samples(monkeypatch, [])
    connect(client)
    page = client.get("/").text
    assert 'data-testid="samples-empty"' in page
    assert 'data-testid="sample-row"' not in page


def test_denied_consent_shows_auth_error(client: TestClient) -> None:
    location = client.get("/connect").headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    client.get("/callback", params={"error": "access_denied", "state": state})
    page = client.get("/").text
    assert 'data-testid="auth-error"' in page
    assert 'data-testid="connect-button"' in page
    # Shown once.
    assert 'data-testid="auth-error"' not in client.get("/").text


def test_invalid_scope_shows_auth_error(client: TestClient) -> None:
    location = client.get("/connect").headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    client.get("/callback", params={"error": "invalid_scope", "state": state})
    assert "invalid_scope" in client.get("/").text


def test_wrong_state_is_rejected(client: TestClient, authkit: FakeAuthKit) -> None:
    client.get("/connect")
    client.get("/callback", params={"code": "code-1", "state": "forged"})
    assert 'data-testid="auth-error"' in client.get("/").text
    assert authkit.token_requests == []


def test_refused_refresh_shows_reconnect_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_samples(monkeypatch, ReconnectRequired())
    connect(client)
    page = client.get("/").text
    assert 'data-testid="reconnect-required"' in page
    assert 'data-testid="connect-button"' in page
    assert 'data-testid="disconnect-button"' not in page


def test_refused_refresh_end_to_end(settings: Settings, authkit: FakeAuthKit) -> None:
    """The real list_samples path: an expired token whose refresh is refused."""
    app = create_app(settings, authkit.client())
    client = TestClient(app, follow_redirects=False)
    connect(client)
    store = cast(SessionStore, app.state.sessions)
    for session in store._sessions.values():  # pyright: ignore[reportPrivateUsage]
        if session.tokens:
            session.tokens.expires_at = 0
    authkit.refresh_error = "invalid_grant"

    page = client.get("/").text
    assert 'data-testid="reconnect-required"' in page
    assert [r["grant_type"] for r in authkit.token_requests][-1] == "refresh_token"


def test_disconnect_forgets_tokens(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_samples(monkeypatch, [])
    connect(client)
    response = client.post("/disconnect")
    assert response.status_code == 303
    page = client.get("/").text
    assert 'data-testid="connect-button"' in page
    assert 'data-testid="granted-scope"' not in page


def test_tool_error_shows_wallet_error(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_samples(monkeypatch, WalletError("This app was not granted the samples:read permission."))
    connect(client)
    page = client.get("/").text
    assert 'data-testid="wallet-error"' in page
    assert "samples:read permission" in page
    assert 'data-testid="disconnect-button"' in page
