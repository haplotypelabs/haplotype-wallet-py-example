# Haplotype Wallet example app (Python)

A small server-side web app that adds a "Connect Haplotype Wallet" button. The user
approves access on the Haplotype Wallet consent screen and comes back to the app, which
then shows the permissions granted and lists the user's samples over MCP.

Use it to see the whole flow working, then copy the parts you need. It is a few hundred
lines of Python, most of them comments: FastAPI, Jinja2 templates, `httpx2` for OAuth, and the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) for the data.

Developer docs: <https://www.haplotype.ai/developers/>

## Quickstart

You need:

- A client ID and client secret for your app, from Haplotype Labs. Ask us to register
  `http://localhost:8787/callback` as a redirect URI.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs Python 3.12
  for you), or Docker.

```sh
git clone https://github.com/haplotypelabs/haplotype-wallet-py-example.git
cd haplotype-wallet-py-example
cp .env.example .env
# Edit .env: set WALLET_CLIENT_ID and WALLET_CLIENT_SECRET.
uv run --env-file .env wallet-example
```

Open <http://localhost:8787> and click the button.

With Docker instead of uv:

```sh
docker build -t wallet-py-example .
docker run --env-file .env -p 8787:8787 wallet-py-example
```

If your credentials are for our stage environment, also set `WALLET_ISSUER` and
`WALLET_MCP_URL` to the stage values listed in `.env.example`.

## Configuration

All settings come from environment variables. `.env.example` lists them.

| Variable | Default | Meaning |
|---|---|---|
| `WALLET_CLIENT_ID` | (required) | Your app's client ID |
| `WALLET_CLIENT_SECRET` | (required) | Your app's client secret. Keep it on the server. |
| `WALLET_ISSUER` | `https://authkit.haplotype.ai` | Authorization server. Endpoints come from its OIDC discovery document. |
| `WALLET_MCP_URL` | `https://api.haplotype.ai/mcp` | MCP endpoint, and the OAuth `resource` |
| `WALLET_SCOPES` | `openid offline_access samples:read` | Scopes to request |
| `APP_URL` | `http://localhost:8787` | Where this app runs. The redirect URI is `APP_URL/callback`. |
| `PORT` | `8787` | Port to listen on |
| `SESSION_SECRET` | random per run | Signs the session cookie |

## How it works

1. **The button** links to `GET /connect` on this app. The browser never talks to the
   wallet's API itself.
2. **`/connect`** creates a session, stores a random `state` and a PKCE verifier in it,
   and redirects to the authorize endpoint with `client_id`, `redirect_uri`, `scope`,
   `state`, the PKCE challenge, and `resource`. See `authorization_url` in
   [`oauth.py`](src/wallet_example/oauth.py).
3. **The user** signs in to Haplotype Wallet and approves the consent screen, which
   lists your app's name and the permissions you asked for. Consent is asked once. Later
   connects with the same scopes come straight back.
4. **`/callback`** checks `state`, then exchanges the code for tokens with a
   server-to-server POST to the token endpoint, sending the client secret, the PKCE
   verifier and `resource` (`exchange_code`). The tokens go into the server-side session.
5. **`/`** shows the granted permissions from the token's scopes, then calls
   the `list_samples` MCP tool with the access token as a bearer token. See
   [`wallet.py`](src/wallet_example/wallet.py).
6. **Before each call** the app refreshes the access token if it expires within a
   minute, and once more if the MCP endpoint answers 401.

The app requests `offline_access` to get a refresh token. Without it, the user has to
connect again every 15 minutes.

## The `resource` trap

Send `resource=<WALLET_MCP_URL>` on the token request **and on every refresh**, not only
on the authorize request.

The authorization server sets the access token's audience (`aud`) from `resource` on the
token request. Without it, the token is issued for a different audience, and the MCP
endpoint rejects it with `401` and `WWW-Authenticate: Bearer error="invalid_token"`. A
first connect that works and a refresh 15 minutes later that silently breaks every call
is the usual symptom. OAuth libraries that refresh tokens for you often leave `resource`
out, which is why this example builds the token requests by hand.

## Client secret and PKCE

Your app is a confidential client: it authenticates to the token endpoint with its
client secret, so the code exchange must run on your server. It also sends PKCE
(`code_challenge` on authorize, `code_verifier` on the code exchange). The authorization
server checks the verifier, and a wrong or missing one fails with `invalid_grant`. Send
both.

## Tokens, refresh and disconnect

- **Tokens stay on the server.** The browser gets a signed, httpOnly cookie that holds a
  random session ID, nothing else. This example keeps sessions in memory, so a restart
  disconnects everyone. A real app stores them in its database, with the tokens
  encrypted.
- **Access tokens last 15 minutes.** The app refreshes them before they expire.
- **When the user disconnects your app in their wallet**, the next refresh fails with
  `400 {"error": "invalid_grant"}`. The app then forgets the tokens and shows "Connect
  again". An access token issued before the disconnect keeps working until it expires,
  at most 15 minutes.
- **The app's own Disconnect button** only makes this app forget the tokens. The grant
  stays in the user's wallet, so connecting again skips the consent screen. The user
  removes the grant under Connected apps in Haplotype Wallet.

## Errors on the callback

The authorization server redirects back with `?error=...&state=...` instead of a code:

- `access_denied`: the user clicked Cancel on the consent screen.
- `invalid_scope`: you asked for a scope that is not assigned to your app. This fails
  before any screen is shown, and there is no `error_description`.

Asking for fewer scopes does not narrow an existing grant: the user keeps what they
approved before. Always read granted permissions from the token, not from what you asked
for. The code exchange response has no `scope` field, so read the access token's `scope`
claim (refresh responses do include `scope`).

## Scopes

| Scope | What the user sees | Tools |
|---|---|---|
| `samples:read` | See which genotype files you have and how they were processed | `list_samples`, `get_sample` |
| `variants:read` | Read your genotype at the positions it asks about | `query_variants` |
| `scores:read` | Read your polygenic scores and analysis runs | `list_member_pgs_scores`, `list_runs`, `get_run` |
| `runs:write` | Start new analyses on your genotypes | `schedule_run` |

Your app can only request the scopes we assigned to it. The
[tool reference](https://www.haplotype.ai/developers/tools/) documents every tool's
input and output.

## Code layout

```
src/wallet_example/
  contract.py   generated from the wallet contract: default URLs, scopes, tool names, result types
  oauth.py      authorize URL, code exchange, refresh
  wallet.py     access tokens for a session, MCP calls
  sessions.py   server-side session store and signed cookie
  app.py        routes and page rendering
  templates/    the page
  static/       CSS and the button (connect-button.svg)
contract/       the vendored wallet contract (connect-contract.json)
scripts/        generate_contract.py, which writes contract.py
tests/          unit tests with a fake authorization server
```

The button, `src/wallet_example/static/connect-button.svg`, is the light version from
https://www.haplotype.ai/developers/connect-button/, which also has the dark version and
the rules for using it.

## Wallet contract

Haplotype Wallet publishes what apps depend on (OAuth and MCP endpoints, scopes and
their wording, tool names, tool input and output schemas) as
[`connect-contract.json`](https://www.haplotype.ai/developers/connect-contract.json).
This repo keeps a copy in `contract/connect-contract.json`, and
`src/wallet_example/contract.py` is generated from it. Do not edit `contract.py` by
hand. Everything else in the app is hand-written.

```sh
uv run scripts/generate_contract.py           # regenerate contract.py
uv run scripts/generate_contract.py --check   # fail if it is out of date (CI runs this)
```

To call another tool, add it to `TOOLS` in `scripts/generate_contract.py` and
regenerate. That adds its name, scope and result TypedDicts.

Updates arrive as pull requests from the `update-contract` workflow. It runs when the
wallet ships a release that changed the contract, daily as a fallback, and on demand
(`gh workflow run update-contract.yml`). It fetches the contract, regenerates, runs the
checks, and opens or updates one PR from the `contract-update` branch when something
changed. It opens the PR with the `CONTRACT_PR_TOKEN` secret when set (a token with
Contents and Pull requests: write on this repo), otherwise with `GITHUB_TOKEN`. If the
contract's `contract_version` changes, the generator stops with an error, because the
shape changed and the generator needs updating first.

## Testing

Page elements carry `data-testid` attributes (`connect-button`, `granted-scope`,
`sample-row`, `samples-empty`, `auth-error`, `reconnect-required`, `wallet-error`,
`disconnect-button`) for automated tests.

With `EXAMPLE_TEST_HOOKS=1` the app also registers `POST /debug/expire-token`. It marks
the current session's access token as expired and answers `204`, so the next page view
runs a refresh. With any other value the route does not exist. Leave it off outside
tests.

## Development

```sh
uv sync
uv run scripts/generate_contract.py --check
uv run ruff format . && uv run ruff check .
uv run pyright
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
