"""Generic GraphQL client.

Knows nothing about any particular query: you hand it a path to a `.graphql`
file, a variables dict and an operation name, and it gives you back the parsed
JSON response body.
"""

import json
from pathlib import Path

import requests

from core.config import Config, load_config
from core.session import TokenSession

DEFAULT_TIMEOUT = 60


class GraphQLError(RuntimeError):
    """The server returned a GraphQL `errors` array."""

    def __init__(self, message: str, errors: list):
        super().__init__(message)
        self.errors = errors


class GraphQLHTTPError(RuntimeError):
    """The server returned a non-200 HTTP status."""

    def __init__(self, message: str, status_code: int, body: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GraphQLClient:
    def __init__(
        self,
        session: TokenSession | None = None,
        config: Config | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self._config = config or load_config()
        self._session = session or TokenSession(self._config)
        self._timeout = timeout

    @staticmethod
    def load_query(query_path: str | Path) -> str:
        """Read the query text out of a `.graphql` file."""
        return Path(query_path).read_text(encoding="utf-8")

    def execute(
        self,
        query_path: str | Path,
        variables: dict,
        operation_name: str,
    ) -> dict:
        """POST a query and return the raw parsed JSON response body.

        Raises:
            GraphQLHTTPError: on any non-200 HTTP status.
            GraphQLError: if the response body carries a non-empty `errors` array.
        """
        payload = {
            "operationName": operation_name,
            "variables": variables,
            "query": self.load_query(query_path),
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._session.auth_headers(),
        }

        response = requests.post(
            self._config.graphql_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=self._timeout,
        )

        if response.status_code != 200:
            raise GraphQLHTTPError(
                f"GraphQL request '{operation_name}' failed with HTTP "
                f"{response.status_code}: {response.text[:2000]}",
                status_code=response.status_code,
                body=response.text,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise GraphQLHTTPError(
                f"GraphQL request '{operation_name}' returned a non-JSON body: "
                f"{response.text[:2000]}",
                status_code=response.status_code,
                body=response.text,
            ) from exc

        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            summary = "; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e)
                for e in errors
            )
            raise GraphQLError(
                f"GraphQL request '{operation_name}' returned errors: {summary}",
                errors=errors,
            )

        return body
