"""Configuration loading.

All settings come from the environment (optionally via a local `.env` file).
Nothing secret is ever hardcoded here.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load a local .env if one exists. Real environment variables always win.
load_dotenv(override=False)

# Override with FAMLY_GRAPHQL_URL if your setting uses a different endpoint.
DEFAULT_GRAPHQL_URL = "https://app.famly.co/graphql"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    access_token: str
    graphql_url: str


def load_config() -> Config:
    """Read configuration from the environment.

    Raises:
        ConfigError: if FAMLY_ACCESS_TOKEN is missing or empty.
    """
    access_token = os.environ.get("FAMLY_ACCESS_TOKEN", "").strip()
    if not access_token:
        raise ConfigError(
            "FAMLY_ACCESS_TOKEN is not set. Copy .env.example to .env and set it, "
            "or export it in your shell."
        )

    graphql_url = os.environ.get("FAMLY_GRAPHQL_URL", "").strip() or DEFAULT_GRAPHQL_URL

    return Config(access_token=access_token, graphql_url=graphql_url)
