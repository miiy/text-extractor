"""Application settings loaded from environment variables."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv

MAX_GENERATION_TOKENS = 8192


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 120
    concurrency: int = 4
    debug: bool = False
    system_prompt: str = "Follow the caller's task prompt and return exactly one valid JSON object."

    def __post_init__(self):
        if not self.base_url.strip():
            raise ValueError("MODEL_BASE_URL is required")
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
            raise ValueError("MODEL_BASE_URL must be an HTTP(S) base URL without query or fragment")
        if not self.model.strip():
            raise ValueError("MODEL_NAME is required")
        if not self.system_prompt.strip() or len(self.system_prompt) > 20_000:
            raise ValueError("SYSTEM_PROMPT must contain between 1 and 20000 characters")
        if not 0 < self.timeout < float("inf") or self.concurrency < 1:
            raise ValueError("MODEL_TIMEOUT and MODEL_CONCURRENCY must be positive")

    @classmethod
    def from_env(cls):
        # Explicit process environment variables take precedence over .env values.
        load_dotenv(dotenv_path=".env", override=False)
        return cls(
            base_url=os.getenv("MODEL_BASE_URL", "").strip().rstrip("/"),
            model=os.getenv("MODEL_NAME", "").strip(),
            api_key=os.getenv("MODEL_API_KEY", "").strip(),
            timeout=float(os.getenv("MODEL_TIMEOUT", "120")),
            concurrency=int(os.getenv("MODEL_CONCURRENCY", "4")),
            debug=os.getenv("DEBUG", "false").strip().lower() in {"true", "1", "yes", "on"},
            system_prompt=os.getenv("SYSTEM_PROMPT", cls.system_prompt),
        )
