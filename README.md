# Senpilot agent

Senpilot provides the modular foundation for an email-driven Nova Scotia regulatory document retrieval agent. It parses requests offline and asks for clarification when a matter number or document category is missing or ambiguous. The UARB Playwright adapter retrieves public documents, and the Gmail worker replies with verified ZIPs and deterministic summaries.

## Setup

Install Python 3.12 and uv, then run from the repository root:

```sh
uv sync --python 3.12
uv run playwright install chromium
uv run python -m src.main "M12205 Other Documents"
uv run python -m src.main --matter M12205 --type "Key Documents" --limit 2 --zip
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

`uv.lock` pins dependencies. Ordinary tests and parsing are offline. Retrieval requires Chromium and access to the public UARB site, but no credentials. Downloads go to ignored `downloads/`; use `--output PATH` or `--headed` as needed. Run the opt-in live check with `UARB_LIVE=1 uv run pytest tests/test_uarb.py -k live`.

## Architecture

- `models.py`: dataclass contracts and validation, including unknown metadata and per-document errors.
- `parser.py`: deterministic ID/category extraction, common aliases, conservative fuzzy matching, and clarification merging. `parse_request` stays offline; `interpret_request` adds pending-request corrections and optional schema-validated OpenRouter recovery.
- `agent.py`: coordinates parsing, clarification, metadata retrieval, capped downloads, packaging, and a stable metadata summary through injected clients.
- `uarb.py`: context-managed Playwright session, live metadata/counts, virtualized document rows, and the GO GET IT / filename-button download flow. Retries once, continues after failures, validates files, and resolves filename collisions.
- `packager.py`: standard-library ZIP creation with filename collision handling and member/content verification.
- `email_client.py`: Desktop OAuth, Gmail MIME/threaded replies, size checks, and a SQLite delivery ledger.
- `config.py`: explicit dotenv loading and lazy, integration-specific validation.
- `main.py`: offline parser and standalone retrieval CLI.

Call `parse_request(text, previous_result)` to supply a clarification. New values fill unresolved fields; conflicting values clear the field and trigger clarification. Repeated identical values are accepted. Only contiguous `M` plus five ASCII digits is valid. Categories include Exhibits, Key Documents, Other Documents, Transcripts, and Recordings; aliases include `key docs`, `other docs`, and `audio`. Fuzzy matches require a score of at least 0.90 and a category margin of 0.12. Plain-text quote/signature markers and explicit negation are handled conservatively; unfamiliar paraphrases may use the optional fallback.

`Agent.process` accepts one incoming message and optional previous parse result. The production worker persists partial requests by thread and normalized sender in the existing SQLite database. Reply subjects and recognized quoted history are excluded from follow-up interpretation. It returns a parse result after sending clarification or a processing result after sending the retrieval summary. Integration failures propagate explicitly. Unknown category counts fail rather than being treated as zero; downloads are limited to ten. Download staging uses per-request temporary directories. With `--zip`, the CLI retains only the verified ZIP in the output directory and prints a deterministic email body; zero downloads produce no archive. Without `--zip`, individual downloads are retained as before. Summaries report exhausted per-document failures separately from delivered files.

## Gmail worker

```sh
uv run python -m src.main --gmail-auth   # Browser authorization; sends nothing
uv run python -m src.main --gmail-check  # Reads new-message count; sends nothing
uv run python -m src.main --poll-once    # Processes one batch and sends replies
uv run python -m src.main --poll         # Runs until Ctrl-C
```

Authorize the configured `AGENT_EMAIL`. OAuth credentials are read from `credentials/google_oauth.json` or a single original `client_secret_*.json` file in that directory. Neither is modified. The verified account's token is stored at `credentials/gmail_token.json` with mode 0600 and refreshed when possible. Tokens revoked or expired beyond refresh require `--gmail-auth` again.

The first successful authentication establishes a persistent cutoff: only messages arriving afterward are eligible, including messages already read or archived. To test, authenticate, run `--gmail-check`, send a new request from a separate account, then run `--poll-once`. The worker processes serially with a process lock. `GMAIL_POLL_INTERVAL` defaults to 15 seconds; existing `.env` values take precedence. Worker request files are removed after sending.

`output/gmail_state.sqlite3` stores mailbox, cutoff, message IDs, delivery status, and validated pending request fields. Conversation transitions commit atomically with successful send receipts; uncertain delivery holds further replies in that conversation. Successful sends become `processed`. A pre-send `sending` entry remains if delivery is uncertain; automatic retries are suppressed. Review the Sent thread before manually resolving such entries. Do not delete the ledger on restart. Read failures retry three times with bounded backoff; ambiguous send failures are never blindly retried. No email bodies, credentials, or tokens are logged.

ZIPs above 18 MB or messages above 24 MB after MIME encoding receive a text-only size explanation. No external download links are substituted. Missing fields get specific clarification, and follow-ups resume pending requests. Explicit corrections such as "Actually, make that Exhibits" replace only uniquely supplied fields. After three unresolved turns, a full request is required. Completed requests clear pending state. Gmail acceptance is tracked, but final recipient delivery must be checked during the live test.

OpenRouter is enabled only when both `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` are set. Normal complete requests make no model calls. The fallback sends only the new cleaned message (maximum 4,000 characters) and validated partial fields, with one call, 256 output tokens, 3/12-second connect/read timeouts, and a 32 KB response cap. Invalid output, timeouts, conflicts, or missing configuration retain deterministic clarification. Strict JSON-schema support is required; no tools or PDF contents are submitted. Model-authored questions are never relayed directly.

For a controlled conversation test, send "Please send the documents for M12205" from a separate account, run `--poll-once`, then reply "Key Documents" in the same thread and run `--poll-once` again. Restarting between commands tests persistence. Set `PLAYWRIGHT_BROWSERS_PATH=/tmp/senpilot-browsers` when using that existing Chromium installation. A new request with unusual wording such as "M12205, the verbatim account of the hearing" exercises the optional fallback. Check the original thread, summary, and attachment; no live emails are sent by unit tests.

## Environment

Preserve your existing `.env` and `credentials/google_oauth.json`. For a new checkout, copy `.env.example` to `.env` and supply local values. Both secrets locations are ignored by Git.

| Variable | Purpose | Required when |
| --- | --- | --- |
| `AGENT_EMAIL` | Agent mailbox address | Gmail adapter is used |
| `GMAIL_POLL_INTERVAL` | Positive polling interval in seconds; default 15 | Gmail adapter is used |
| `OPENROUTER_API_KEY` | OpenRouter credential | Optional LLM fallback is used |
| `OPENROUTER_MODEL` | Provider/model identifier | Optional LLM fallback is used |

`Config.from_env()` loads dotenv without overriding existing environment values. Nothing loads credentials at import time. `validate_gmail()` checks settings and the Desktop OAuth file's existence, without opening it; `validate_openrouter()` checks only the optional LLM settings. Neither method authenticates.

## Milestone status and remaining work

Implemented: parsing, live UARB retrieval, verified ZIP packaging, deterministic summaries, Gmail authentication/polling/replies, persistent clarification, and optional OpenRouter interpretation. The adapter uses DOM selectors inspected on the live FileMaker layouts; site layout changes may require selector updates. Use `with UARBClient() as client` or call `close()` after retrieval. Downloads stop at the requested successful count (maximum ten); exhausted per-document failures are returned separately. Unknown metadata remains unknown; unreadable counts fail explicitly.

Unimplemented: deployment. Live Gmail/model verification requires authorization, a separate-account test email, and a compatible configured OpenRouter model.

Offline tests cover parsing, models, orchestration, download validation, collisions and retry behavior. The opt-in browser test checks real metadata and one PDF without hardcoding changing counts.
