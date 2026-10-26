FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache --python 3.12 \
    && .venv/bin/python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY src/ ./src/

ENV UV_NO_SYNC=1 \
    UV_NO_DEV=1 \
    GOOGLE_OAUTH_PATH=/data/google_oauth.json \
    GMAIL_TOKEN_PATH=/data/gmail_token.json \
    GMAIL_STATE_PATH=/data/gmail_state.sqlite3 \
    GMAIL_ALLOW_INTERACTIVE_AUTH=false \
    SENPILOT_OUTPUT_DIR=/tmp/senpilot

STOPSIGNAL SIGTERM
CMD ["uv", "run", "python", "-m", "src.main", "--poll"]
