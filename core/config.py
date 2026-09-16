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

# Base for the REST API (paths like /v2/plans hang off this). Override with
# FAMLY_REST_BASE_URL.
DEFAULT_REST_BASE_URL = "https://app.famly.co/api"

# Sentinel for COMMIT_ALLOWED_CHILD_IDS meaning "any child". Deliberately not
# the default: the allow-list is the guard that keeps an experimental write off
# a real child, so opening it has to be a typed, visible decision.
ALLOW_ALL_CHILDREN = "*"

# Values accepted as true for boolean env vars.
TRUE_VALUES = ("1", "true", "yes", "on")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    access_token: str
    graphql_url: str
    rest_base_url: str
    # Master switch for writing to Famly. False by default: commit stays off
    # until it is deliberately turned on.
    commit_enabled: bool = False
    # Child IDs a commit may write to. Empty means no commit can proceed --
    # runner.commit refuses an empty allow-list.
    commit_allowed_child_ids: frozenset = frozenset()

    @property
    def commit_allows_any_child(self) -> bool:
        """True when the allow-list has been opened to every child."""
        return ALLOW_ALL_CHILDREN in self.commit_allowed_child_ids

    def commit_allows(self, child_id: str | None) -> bool:
        """Whether a commit to `child_id` is permitted by configuration."""
        if not child_id:
            return False
        if self.commit_allows_any_child:
            return True
        return child_id in self.commit_allowed_child_ids


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

    rest_base_url = (
        os.environ.get("FAMLY_REST_BASE_URL", "").strip() or DEFAULT_REST_BASE_URL
    )
    # Paths are joined with a leading slash, so never keep a trailing one here.
    rest_base_url = rest_base_url.rstrip("/")

    commit_enabled = (
        os.environ.get("COMMIT_ENABLED", "").strip().lower() in TRUE_VALUES
    )

    commit_allowed_child_ids = frozenset(
        item.strip()
        for item in os.environ.get("COMMIT_ALLOWED_CHILD_IDS", "").split(",")
        if item.strip()
    )

    return Config(
        access_token=access_token,
        graphql_url=graphql_url,
        rest_base_url=rest_base_url,
        commit_enabled=commit_enabled,
        commit_allowed_child_ids=commit_allowed_child_ids,
    )
