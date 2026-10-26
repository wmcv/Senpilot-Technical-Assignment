# Senpilot Document Agent

An AI-assisted email agent that retrieves regulatory filings from the [Nova Scotia Regulatory and Appeals Board](https://uarb.novascotia.ca/fmi/webd/UARB15).

Email a matter number and document category to the agent. It retrieves up to 10 documents and replies with a ZIP attachment and a summary of the matter's metadata.

## Features

- **Email-driven requests:** Receive and respond to requests through Gmail.
- **Intelligent parsing:** Deterministic extraction with Claude Haiku 5.5 fallback via OpenRouter.
- **Conversational clarification:** Resolve missing or ambiguous information through email replies.
- **Automated retrieval:** Navigate the UARB database using Playwright.
- **Document packaging:** Download, validate, and ZIP up to 10 documents.
- **Reliable processing:** Retry failed downloads, prevent duplicate responses, and persist conversation state.

## Tech Stack

Python 3.12 · Playwright · Gmail API · OpenRouter · SQLite · uv

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
```

Copy `.env.example` to `.env` and configure Gmail and OpenRouter credentials. Place your Google Desktop OAuth JSON file in `credentials/google_oauth.json`.

## Usage

Retrieve documents directly:

```bash
uv run python -m src.main --matter M12205 --type "Other Documents" --limit 10 --zip
```

Authorize Gmail:

```bash
uv run python -m src.main --gmail-auth
```

Start the email agent:

```bash
uv run python -m src.main --poll
```

## Tests

```bash
uv run pytest
uv run ruff check .
```

The test suite covers request interpretation, conversational clarification, document retrieval, ZIP integrity, and email processing. External integrations are mocked during ordinary tests.

## Docker / Railway worker

The Dockerfile uses Python 3.12, locked uv dependencies, and the matching Playwright Chromium with Linux dependencies. Its command is `uv run python -m src.main --poll`; Chromium runs headless. Build and test the browser without network access or sending mail:

```bash
docker build -t senpilot-worker .
docker run --rm --network none -i senpilot-worker uv run python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    print(browser.version)
    browser.close()
PY
```

On Railway, use the Dockerfile, exactly one replica, a persistent volume mounted at `/data`, and no public domain or HTTP health check. Set these service variables:

| Variable | Value |
| --- | --- |
| `AGENT_EMAIL` | Authorized Gmail mailbox (required) |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `60` to allow SIGTERM cleanup |
| `OPENROUTER_API_KEY` | Runtime secret, optional for fallback |
| `OPENROUTER_MODEL` | `anthropic/claude-haiku-5.5`, if using fallback |
| `GMAIL_POLL_INTERVAL` | Optional; defaults to `15` seconds |

Image defaults are `GOOGLE_OAUTH_PATH=/data/google_oauth.json`, `GMAIL_TOKEN_PATH=/data/gmail_token.json`, `GMAIL_STATE_PATH=/data/gmail_state.sqlite3`, and `GMAIL_ALLOW_INTERACTIVE_AUTH=false`. Downloads and ZIPs use per-request temporary directories under `SENPILOT_OUTPUT_DIR=/tmp/senpilot` and are removed after processing. Keep the image's `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright`; do not copy a local macOS browser path.

Provision secrets after attaching the volume, never through image layers or build arguments:

1. Stop the local poller. Temporarily set the Railway Start Command to `sleep infinity` so the volume can be provisioned before the worker starts.
2. Register your SSH key and use [Railway SCP/SFTP](https://docs.railway.com/cli/ssh#copy-files-with-scp-and-sftp) to upload the Google Desktop OAuth JSON to `/data/google_oauth.json` and the already-authorized local Gmail token to `/data/gmail_token.json`. For a service without a domain, use its **Service Instance ID** as the SSH username. Do not paste file contents into logs or commands.
3. Copy the existing `output/gmail_state.sqlite3` while the local worker is stopped to `/data/gmail_state.sqlite3` to preserve handled IDs, pending conversations, and the first-run cutoff. Restrict uploaded files to mode `600` using SSH.
4. Clear the temporary Start Command to use the Dockerfile command. Keep one worker and retain the same volume across redeployments. Missing or invalid authorization fails explicitly; authorize locally and replace the token rather than running interactive OAuth in the container.

SIGTERM unwinds browser, temporary-directory, and polling-lock cleanup. An interrupted send retains the existing uncertain-delivery claim to prevent duplicates. Configure the [Railway shutdown grace period](https://docs.railway.com/deployments/reference) above; the default does not allow cleanup time.

Google OAuth apps with external **Testing** status generally issue [refresh tokens that expire after seven days](https://developers.google.com/identity/protocols/oauth2#expiration) for Gmail scopes. Check the app's publishing status before relying on unattended operation. Local CLI paths and interactive authorization defaults remain unchanged.

## Supported Documents

Exhibits · Key Documents · Other Documents · Transcripts · Recordings
