# leboncoin-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant work with your
[Leboncoin](https://www.leboncoin.fr) account through a real, logged-in Chrome.

It runs as two Docker containers: a headed Google Chrome that you log in to once (through
your web browser, via noVNC), and the MCP server, which drives that Chrome over the DevTools
protocol. Your assistant only sees the MCP tools.

```text
MCP client ──HTTP──> mcp ──CDP──> browser (Chrome) ──> leboncoin.fr
                                     ^
you ──noVNC (login, CAPTCHA)─────────┘
```

> [!WARNING]
> This is an unofficial project, not affiliated with or endorsed by Leboncoin. Automating
> your account may go against Leboncoin's terms of use and can get it challenged or blocked.
> Use it on your own account, keep the volume low, and at your own risk.

## Status

Early, read-only. Available tools:

| Tool | What it does |
| --- | --- |
| `session_status` | Checks that the session works; tells you what to do if it does not. |
| `list_conversations` | The 50 most recent conversations, optionally unread only. |
| `get_conversation` | One conversation's history, oldest first. May mark it as read. Says when the person shared a tenant profile. |
| `get_tenant_profile` | A tenant profile (« profil locataire ») a candidate shared for one of your rentals: contact details, income, job, guarantor, household, move-in wishes. |

Planned: sending messages, managing and publishing ads, more of the landlord space
(« espace bailleur »).

## Principles

- **No CAPTCHA or 2FA automation.** When Leboncoin shows a challenge, the tool stops and asks
  you to solve it in noVNC.
- **No paid options.** The server never buys boosts or other paid features.
- **Looks like a person, not a scraper.** It reads what the real pages load for themselves
  instead of calling Leboncoin's API directly, runs one browser operation at a time, and never
  retries a refusal.
- **Your session stays yours.** Cookies and auth headers are never logged or returned.
- **Other people's data stays private.** Messages and tenant profiles are returned to your
  assistant only, never logged or stored by the server.

## Requirements

- Docker with Compose.
- An x86_64 machine (the browser image uses Google Chrome for Linux, which has no ARM build).

## Quick start

```sh
git clone <this repository> leboncoin-mcp
cd leboncoin-mcp
docker compose up -d --build --wait
```

1. Open <http://localhost:6080/vnc.html?autoconnect=1&resize=scale>. You see Chrome on
   leboncoin.fr.
2. Log in to your account as usual. To paste a password, use noVNC's clipboard panel (left
   sidebar), then **Ctrl+V** in the remote Chrome.
3. Connect your MCP client to `http://127.0.0.1:8000/mcp` (streamable HTTP). With Claude Code:

   ```sh
   claude mcp add --transport http leboncoin http://127.0.0.1:8000/mcp
   ```

4. Ask your assistant to check the session, then to list your conversations.

The Chrome profile is kept in the `chrome-profile` Docker volume, so you stay logged in across
restarts. Log in again in noVNC whenever `session_status` says the session expired.

## Security

- Both ports are bound to `127.0.0.1` only. The Chrome DevTools port is not published at all:
  anyone who reaches it controls your logged-in browser.
- Requests whose `Host` or `Origin` header is not local are refused, so a web page you visit
  cannot reach the server through your browser (DNS rebinding). Reaching it under another name
  requires adding that name to `LEBONCOIN_MCP_ALLOWED_HOSTS`.
- **The MCP endpoint has no authentication yet.** Do not expose it beyond your machine, and do
  not publish noVNC either: it shows your logged-in session without a password.
- The browser container gets `SYS_ADMIN` so Chrome can keep its sandbox. Chrome itself runs as
  a non-root user. The alternative, `--no-sandbox`, would disable Chrome's isolation between web
  pages and your session.

## Configuration

Set in `compose.yaml` (`mcp` service):

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEBONCOIN_MCP_CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools endpoint |
| `LEBONCOIN_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `LEBONCOIN_MCP_HOST` | `127.0.0.1` | HTTP listen address |
| `LEBONCOIN_MCP_PORT` | `8000` | HTTP listen port |
| `LEBONCOIN_MCP_ALLOWED_HOSTS` | *(none)* | Extra accepted `Host` headers, comma-separated (`name:*` = any port). Local hosts are always accepted. |
| `LEBONCOIN_MCP_NOVNC_URL` | `http://localhost:6080/vnc.html?…` | URL shown in "log in" / "solve the CAPTCHA" errors |

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check src tests
```

The tests are pure parsing tests on synthetic payloads: they need no network and no account.
Never commit real Leboncoin responses: they contain other people's names and messages.

## License

[MIT](LICENSE)
