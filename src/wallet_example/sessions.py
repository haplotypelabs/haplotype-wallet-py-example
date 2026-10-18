"""Server-side sessions. Tokens stay on the server; the browser only holds a session ID.

The cookie carries a random session ID signed with SESSION_SECRET. Everything else,
including the access and refresh tokens, sits in process memory. That is enough for an
example. A real app keeps sessions in its database or cache, with the tokens encrypted.
"""

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from wallet_example.oauth import TokenSet

COOKIE_NAME = "wallet_example_session"


@dataclass
class Session:
    oauth_state: str | None = None
    """`state` sent on the pending authorize request, checked on the callback."""
    code_verifier: str | None = None
    """PKCE verifier for the pending authorize request."""
    tokens: TokenSet | None = None
    auth_error: str | None = None
    """Message for the next page view after a failed or denied connect."""
    reconnect_required: bool = False
    """Set when a refresh was refused, until the user connects again."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serializes refreshes, so parallel requests do not refresh twice."""


class SessionStore:
    def __init__(self, secret: str) -> None:
        self._key = secret.encode()
        self._sessions: dict[str, Session] = {}

    def _sign(self, session_id: str) -> str:
        return hmac.new(self._key, session_id.encode(), hashlib.sha256).hexdigest()

    def get(self, cookie: str | None) -> Session | None:
        if not cookie or "." not in cookie:
            return None
        session_id, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(session_id)):
            return None
        return self._sessions.get(session_id)

    def create(self) -> tuple[Session, str]:
        """Create a session and return it with the cookie value that points to it."""
        session_id = secrets.token_urlsafe(32)
        session = Session()
        self._sessions[session_id] = session
        return session, f"{session_id}.{self._sign(session_id)}"
