"""Token attachment for outgoing Famly requests.

Deliberately thin: today this only takes the configured access token and turns
it into request headers. It is NOT an authentication layer -- there is no login
flow, no credential handling and no token lifecycle management here yet.
"""

from core.config import Config, load_config


class TokenSession:
    """Holds the access token and produces the headers that carry it.

    The token is supplied by configuration. If it expires, requests will start
    failing and a new token must be provided out-of-band (see `refresh`).
    """

    def __init__(self, config: Config | None = None):
        self._config = config or load_config()
        self._access_token = self._config.access_token

    @property
    def access_token(self) -> str:
        return self._access_token

    def auth_headers(self) -> dict[str, str]:
        """Headers that authenticate a request to Famly."""
        return {"x-famly-accesstoken": self._access_token}

    def refresh(self) -> None:
        """SEAM: future token-refresh / login flow hooks in here.

        Intentionally unimplemented. When a real login or refresh flow is added,
        it should replace `self._access_token` in place so that callers holding
        this session keep working without changes. Until then, an expired token
        is a configuration problem to be fixed by setting a new
        FAMLY_ACCESS_TOKEN.
        """
        raise NotImplementedError(
            "Token refresh is not implemented. Set a new FAMLY_ACCESS_TOKEN."
        )
