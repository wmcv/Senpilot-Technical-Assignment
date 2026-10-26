"""Environment loading without import-time credential requirements."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    agent_email: str | None = None
    openrouter_api_key: str | None = field(default=None, repr=False)
    openrouter_model: str | None = None
    gmail_poll_interval: str = "15"
    google_oauth_path: Path = Path("credentials/google_oauth.json")

    gmail_token_path: Path = Path("credentials/gmail_token.json")
    gmail_state_path: Path = Path("output/gmail_state.sqlite3")
    gmail_allow_interactive_auth: bool = True

    @property
    def oauth_credentials_path(self) -> Path:
        if self.google_oauth_path.is_file() or self.google_oauth_path != Path(
            "credentials/google_oauth.json"
        ):
            return self.google_oauth_path
        # Preserve the original downloaded Desktop OAuth file without renaming it.
        candidates = list(self.google_oauth_path.parent.glob("client_secret_*.json"))
        if len(candidates) == 1:
            return candidates[0]
        return self.google_oauth_path

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")) -> "Config":
        load_dotenv(env_file, override=False)
        return cls(
            agent_email=os.getenv("AGENT_EMAIL"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
            openrouter_model=os.getenv("OPENROUTER_MODEL"),
            gmail_poll_interval=os.getenv("GMAIL_POLL_INTERVAL", "15"),
            google_oauth_path=Path(os.getenv("GOOGLE_OAUTH_PATH", "credentials/google_oauth.json")),
            gmail_token_path=Path(os.getenv("GMAIL_TOKEN_PATH", "credentials/gmail_token.json")),
            gmail_state_path=Path(os.getenv("GMAIL_STATE_PATH", "output/gmail_state.sqlite3")),
            gmail_allow_interactive_auth=os.getenv("GMAIL_ALLOW_INTERACTIVE_AUTH", "true").lower()
            in {"1", "true", "yes"},
        )

    def validate_gmail(self) -> int:
        if not self.agent_email or not self.agent_email.strip():
            raise ValueError("AGENT_EMAIL is required for Gmail")
        if not self.oauth_credentials_path.is_file():
            raise ValueError("Google Desktop OAuth credentials file is required for Gmail")
        try:
            interval = int(self.gmail_poll_interval)
        except ValueError:
            raise ValueError("GMAIL_POLL_INTERVAL must be a positive integer") from None
        if interval <= 0:
            raise ValueError("GMAIL_POLL_INTERVAL must be a positive integer")
        return interval

    def validate_openrouter(self) -> None:
        if not all(
            value and value.strip()
            for value in (
                self.openrouter_api_key,
                self.openrouter_model,
            )
        ):
            raise ValueError("OPENROUTER_API_KEY and OPENROUTER_MODEL are required for OpenRouter")
