"""A fake AuthKit token server, so OAuth logic runs without network access."""

import base64
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx2
import pytest

from wallet_example.config import Settings

ISSUER = "https://auth.example.test"
MCP_URL = "https://api.example.test/mcp"


def fake_jwt(claims: dict[str, object]) -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


@dataclass
class FakeAuthKit:
    """Records token requests and answers them like AuthKit does."""

    token_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    refresh_error: str | None = None
    include_scope: bool = True
    issued: int = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth2/authorize",
                    "token_endpoint": f"{ISSUER}/oauth2/token",
                },
            )
        if request.url.path == "/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if form["grant_type"] == "refresh_token" and self.refresh_error:
                return httpx2.Response(400, json={"error": self.refresh_error})
            # Mirror the real server: the audience comes from `resource` on this request.
            aud = form.get("resource", "client_main")
            scope = "openid offline_access samples:read scores:read"
            self.issued += 1
            body: dict[str, object] = {
                "access_token": fake_jwt({"aud": aud, "scope": scope, "n": self.issued}),
                "refresh_token": f"refresh-{self.issued}",
                "token_type": "bearer",
                "expires_in": 900,
            }
            if self.include_scope:
                body["scope"] = scope
            return httpx2.Response(200, json=body)
        return httpx2.Response(404)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))


@pytest.fixture
def authkit() -> FakeAuthKit:
    return FakeAuthKit()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        issuer=ISSUER,
        mcp_url=MCP_URL,
        client_id="client_test",
        client_secret="secret_test",
        scopes="openid offline_access samples:read",
        app_url="http://localhost:8787",
        port=8787,
        session_secret="session-secret",
    )
