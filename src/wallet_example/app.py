"""The web app: a home page, the OAuth redirect and callback, and disconnect."""

import hmac
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wallet_example.config import Settings, load_settings
from wallet_example.contract import SCOPE_DESCRIPTIONS, SampleRecord
from wallet_example.oauth import OAuthError, WalletOAuth, new_pkce_pair
from wallet_example.sessions import COOKIE_NAME, Session, SessionStore
from wallet_example.wallet import ReconnectRequired, WalletError, list_samples
from wallet_example.wallet_errors import WalletErrorView, wallet_error_view

HERE = Path(__file__).parent

# Standard OpenID Connect scopes. They are not wallet permissions, so the page leaves them out.
OIDC_SCOPES = {"openid", "profile", "email", "offline_access"}

CALLBACK_ERRORS = {
    "access_denied": "You did not allow access to your Haplotype Wallet.",
    "invalid_scope": (
        "This app asked for a permission it is not set up for (invalid_scope). "
        "Check WALLET_SCOPES against the scopes assigned to your app."
    ),
}


def format_date(value: str | None) -> str:
    """Format an ISO timestamp as a short date, e.g. 18 Oct 2026."""
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y") if value else ""
    except ValueError:
        return value or ""


def create_app(settings: Settings | None = None, http: httpx2.AsyncClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    http = http or httpx2.AsyncClient(timeout=httpx2.Timeout(15))
    oauth = WalletOAuth(settings, http)
    sessions = SessionStore(settings.session_secret)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["date"] = format_date

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.sessions = sessions  # for tests

    def current_session(request: Request) -> Session | None:
        return sessions.get(request.cookies.get(COOKIE_NAME))

    def redirect(url: str, new_cookie: str | None = None) -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        if new_cookie:
            # httpOnly: page scripts cannot read it. SameSite=Lax: other sites cannot
            # POST to /disconnect with it.
            response.set_cookie(
                COOKIE_NAME,
                new_cookie,
                httponly=True,
                samesite="lax",
                secure=settings.secure_cookies,
            )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        session = current_session(request)
        auth_error = session.auth_error if session else None
        reconnect_required = bool(session and session.reconnect_required)
        granted: list[tuple[str, str]] = []
        samples: list[SampleRecord] | None = None
        wallet_error: WalletErrorView | None = None

        if session:
            session.auth_error = None  # show it once
        if session and session.tokens:
            granted = [
                (scope, SCOPE_DESCRIPTIONS.get(scope, scope))
                for scope in session.tokens.scope.split()
                if scope not in OIDC_SCOPES
            ]
            # Called even without `samples:read`: the wallet then answers
            # `missing_scope`, and the page offers to connect again for it.
            try:
                samples = await list_samples(settings, session, oauth)
            except ReconnectRequired:
                session.tokens = None
                session.reconnect_required = True
                reconnect_required = True
            except WalletError as e:
                wallet_error = wallet_error_view(e, settings.app_url)

        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "connected": bool(session and session.tokens),
                "auth_error": auth_error,
                "reconnect_required": reconnect_required,
                "granted": granted,
                "samples": samples,
                "wallet_error": wallet_error,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/connect")
    async def connect(request: Request, add_scope: str | None = None) -> Response:
        session = current_session(request)
        new_cookie = None
        if session is None:
            session, new_cookie = sessions.create()
        # `add_scope` asks for one more wallet scope than configured, after a tool call
        # failed with `missing_scope`. The consent screen shows again.
        scopes = (session.test_scopes or settings.scopes).split()
        if add_scope in SCOPE_DESCRIPTIONS and add_scope not in scopes:
            scopes.append(add_scope)
        # `state` ties the callback to this browser session (CSRF protection).
        session.oauth_state = secrets.token_urlsafe(32)
        session.code_verifier, code_challenge = new_pkce_pair()
        try:
            url = await oauth.authorization_url(
                session.oauth_state, code_challenge, scope=" ".join(scopes)
            )
        except httpx2.HTTPError as e:
            session.auth_error = f"Could not reach Haplotype Wallet ({e})."
            url = "/"
        return redirect(url, new_cookie)

    @app.get("/callback")
    async def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> Response:
        if code is None and state is None and error is None:
            # Back from the wallet's account page (see `return_address` in
            # wallet_errors.py), not from a connect attempt.
            return redirect("/")
        session = current_session(request)
        new_cookie = None
        if session is None:
            session, new_cookie = sessions.create()
        expected_state, code_verifier = session.oauth_state, session.code_verifier
        session.oauth_state = session.code_verifier = None  # one use only

        if not (state and expected_state and hmac.compare_digest(state, expected_state)):
            session.auth_error = "This connect attempt expired or did not start here. Try again."
        elif error:
            session.auth_error = CALLBACK_ERRORS.get(error) or (
                f"Haplotype Wallet returned an error: {error_description or error}."
            )
        elif not code or not code_verifier:
            session.auth_error = "Haplotype Wallet did not return an authorization code."
        else:
            try:
                session.tokens = await oauth.exchange_code(code, code_verifier)
                session.reconnect_required = False
            except (OAuthError, httpx2.HTTPError) as e:
                session.auth_error = f"Could not finish connecting ({e})."
        return redirect("/", new_cookie)

    @app.post("/disconnect")
    async def disconnect(request: Request) -> Response:
        # Forgets the tokens in this app only. The grant stays in the user's wallet until
        # they remove the app there; AuthKit has no token revocation endpoint.
        session = current_session(request)
        if session:
            session.tokens = None
            session.reconnect_required = False
        return redirect("/")

    if settings.test_hooks:

        @app.post("/debug/expire-token", status_code=204)
        async def expire_token(request: Request) -> Response:
            """Test-only: mark the access token as expired, so the next page view refreshes it."""
            session = current_session(request)
            if session and session.tokens:
                session.tokens.expires_at = 0
            return Response(status_code=204)

        @app.post("/debug/scopes", status_code=204)
        async def set_scopes(request: Request) -> Response:
            """Test-only: the scopes the next /connect in this session asks for.

            With fewer scopes than a tool needs, a test can reach the `missing_scope` path.
            """
            form = parse_qs((await request.body()).decode())
            scopes = " ".join(form.get("scopes", [""])).strip()
            if not scopes:
                return PlainTextResponse("scopes is required", status_code=400)
            new_cookie = None
            session = current_session(request)
            if session is None:
                session, new_cookie = sessions.create()
            session.test_scopes = scopes
            response = Response(status_code=204)
            if new_cookie:
                response.set_cookie(
                    COOKIE_NAME,
                    new_cookie,
                    httponly=True,
                    samesite="lax",
                    secure=settings.secure_cookies,
                )
            return response

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    return app
