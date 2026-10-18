"""What to show when a tool call fails.

The wallet sends a stable code with every error (`ToolError.info`), and the app acts on
the code, never on the message text, which is written for AI assistants.

- billing codes: "Renew your Haplotype Wallet plan", a link to the wallet's account page,
  and a way back here
- `missing_scope`: offer to connect again, asking for that scope
- retryable codes: "try again"
- anything else: the message and the error reference

Codes: https://www.haplotype.ai/developers/connect-wallet/#in-a-tool-result
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from wallet_example.contract import SCOPE_DESCRIPTIONS
from wallet_example.wallet import ToolError, WalletError

BILLING_MESSAGES = {
    "trial_ended": "Your Haplotype Wallet free trial has ended. Subscribe to keep using it here.",
    "subscription_ended": (
        "Your Haplotype Wallet subscription has ended. Renew it to keep using it here."
    ),
    "limit_reached": (
        "This would go over what your Haplotype Wallet plan includes. Change your plan to continue."
    ),
}


@dataclass(frozen=True)
class WalletErrorView:
    kind: Literal["billing", "missing-scope", "retry", "other"]
    code: str | None
    """The wallet's error code. None when the wallet could not be reached at all."""
    message: str = ""
    renew_url: str | None = None
    """Billing: the wallet's account page, with a way back to this app."""
    scope: str | None = None
    description: str | None = None
    """Missing scope: the consent screen's wording for it."""
    reconnect_url: str | None = None
    """Missing scope: starts the connect flow again, asking for the scope too."""
    ref: str | None = None


def return_address(app_url: str) -> str:
    """Where the user returns after managing their plan.

    The wallet's account page accepts only one of this app's registered redirect URIs,
    and the callback is the one this app always has.
    """
    return f"{app_url}/callback"


def with_query(url: str, **params: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in params]
    return urlunsplit(parts._replace(query=urlencode([*query, *params.items()])))


def wallet_error_view(error: WalletError, app_url: str) -> WalletErrorView:
    if not isinstance(error, ToolError):
        # The wallet could not be reached: nothing to act on but a retry.
        return WalletErrorView(kind="retry", code=None)
    info = error.info

    if info.code in BILLING_MESSAGES:
        renew_url = (
            with_query(info.manage_url, return_to=return_address(app_url))
            if info.manage_url
            else None
        )
        return WalletErrorView(
            kind="billing",
            code=info.code,
            message=BILLING_MESSAGES[info.code],
            renew_url=renew_url,
        )

    if info.code == "missing_scope" and info.scope in SCOPE_DESCRIPTIONS:
        return WalletErrorView(
            kind="missing-scope",
            code=info.code,
            scope=info.scope,
            description=SCOPE_DESCRIPTIONS[info.scope],
            reconnect_url="/connect?" + urlencode({"add_scope": info.scope}),
        )

    if info.retryable:
        return WalletErrorView(kind="retry", code=info.code)

    # The wallet's message ends with the reference and a hint for AI assistants.
    # People get the first part, and the reference on its own.
    return WalletErrorView(
        kind="other", code=info.code, message=str(error).split(" (ref: ")[0], ref=info.ref
    )
