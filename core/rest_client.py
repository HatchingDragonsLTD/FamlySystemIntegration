"""Generic REST client.

Knows nothing about any particular endpoint: you hand it a path, query params
and an optional JSON body, and it gives you back the parsed JSON response.

Mirrors `core/client.py` (the GraphQL client) in shape and error style -- same
TokenSession for auth, same raise-on-failure policy, same body truncation in
error messages.
"""

import json
from typing import Any

import requests

from core.config import Config, load_config
from core.session import TokenSession

DEFAULT_TIMEOUT = 60

# How much of a failing response body to include in the error message.
ERROR_BODY_LIMIT = 2000


class RestHTTPError(RuntimeError):
    """The server returned a non-2xx status, or a body that was not JSON."""

    def __init__(self, message: str, status_code: int, body: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RestClient:
    def __init__(
        self,
        session: TokenSession | None = None,
        config: Config | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self._config = config or load_config()
        self._session = session or TokenSession(self._config)
        self._timeout = timeout

    def _url(self, path: str) -> str:
        """Join `path` onto the configured REST base URL."""
        return f"{self._config.rest_base_url}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        """Auth plus JSON content negotiation -- nothing else.

        The captured requests carry no other headers, so none are added here.
        """
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._session.auth_headers(),
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: Any | None = None,
    ) -> Any:
        """Send a request and return the parsed JSON response.

        Raises:
            RestHTTPError: on any non-2xx status, or if the body is not JSON.
        """
        url = self._url(path)

        data = None
        if json_body is not None:
            data = json.dumps(json_body)

        response = requests.request(
            method,
            url,
            headers=self._headers(),
            params=params,
            data=data,
            timeout=self._timeout,
        )

        if not 200 <= response.status_code < 300:
            raise RestHTTPError(
                f"{method} {url} failed with HTTP {response.status_code}: "
                f"{response.text[:ERROR_BODY_LIMIT]}",
                status_code=response.status_code,
                body=response.text,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise RestHTTPError(
                f"{method} {url} returned a non-JSON body: "
                f"{response.text[:ERROR_BODY_LIMIT]}",
                status_code=response.status_code,
                body=response.text,
            ) from exc

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET `path` and return the parsed JSON response."""
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        params: dict | None = None,
        json_body: Any | None = None,
    ) -> Any:
        """POST `json_body` to `path` and return the parsed JSON response."""
        return self._request("POST", path, params=params, json_body=json_body)
