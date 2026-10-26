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

## Supported Documents

Exhibits · Key Documents · Other Documents · Transcripts · Recordings
