"""The ONLY Famly writes phase 2 makes: create a child, create a contact.

There is no delete, update or batch path in this module, and the two GraphQL
documents are checked at import-time use (`_assert_create_only`) to contain a
`create(` call and nothing that deletes or updates.

NEVER RETRY A CREATE. Each function sends ONE request and classifies the outcome:

  * accepted                       -> the new id.
  * REJECTED (`CreateRejected`)    -> Famly refused it and nothing was written:
        any GraphQL error, and any HTTP 4xx. HTTP 400 with display "Transient"
        is NOT transient (it is a variable-coercion error): it is not retried.
  * UNKNOWN (`OutcomeUnknown`)     -> the request may or may not have landed:
        a timeout, a connection error, an HTTP 5xx, or a body that is not JSON.
        The caller reads Famly (a live lookup) to find out; it never re-sends.

Error text is not carried out of here: a rejected request becomes a short code,
so no response body (which could echo a name or an email) reaches a log, the
jobs table or Slack.
"""

import json
from pathlib import Path

import requests

from core.client import GraphQLError, GraphQLHTTPError

HERE = Path(__file__).parent
CREATE_CHILD_QUERY = HERE / "create_child.graphql"
CREATE_CONTACT_QUERY = HERE / "create_contact.graphql"
CREATE_CHILD_OP = "OnboardChildCreate"
CREATE_CONTACT_OP = "OnboardContactCreate"


class CreateRejected(Exception):
    """Famly refused the create; nothing was written. `code` is a short code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class OutcomeUnknown(Exception):
    """The create may or may not have landed. Look; never re-send."""


def _assert_create_only(path: Path) -> None:
    text = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ).lower()
    if "create(" not in text or "delete" in text or "update(" in text:
        raise AssertionError(f"{path.name} is not a create-only document")


def _rows(body, key: str) -> list[str]:
    """The created rows' ids in a response body (a dict or JSON text), or []."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return []
    try:
        rows = body["data"][key]["create"]
        return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]
    except (KeyError, TypeError, IndexError):
        return []


def _send(client, path: Path, operation: str, variables: dict, kind: str, key: str):
    """One request. Returns the created rows' ids.

    AN HTTP ERROR OR `errors` IN THE BODY DOES NOT MEAN THE MUTATION DID NOT RUN. A
    response that carries created rows is a creation, whatever else it carries. One
    that carries none is `CreateRejected` (a 4xx, or errors) or `OutcomeUnknown` (5xx,
    timeout, malformed); the CALLER settles either by a live lookup before it reports a
    failure. Nothing here ever sends twice."""
    _assert_create_only(path)
    try:
        body = client.execute(query_path=path, variables=variables, operation_name=operation)
    except GraphQLHTTPError as exc:
        ids = _rows(exc.body, key)
        if ids:
            return ids
        if exc.status_code >= 500:
            raise OutcomeUnknown() from None
        raise CreateRejected(f"{kind}_http_{exc.status_code}") from None
    except GraphQLError as exc:
        ids = _rows(getattr(exc, "body", None), key)
        if ids:
            return ids
        raise CreateRejected(f"{kind}_graphql_error") from None
    except (requests.RequestException, TimeoutError, ConnectionError):
        raise OutcomeUnknown() from None

    ids = _rows(body, key)
    if not ids:
        raise OutcomeUnknown() from None
    return ids


def create_child(client, child_input: dict) -> str:
    """Create ONE child. Returns its id.

    Raises:
        CreateRejected: refused; nothing written.
        OutcomeUnknown: may have landed; the caller must look.
    """
    ids = _send(client, CREATE_CHILD_QUERY, CREATE_CHILD_OP,
                {"children": [child_input]}, "child", "children")
    if len(ids) != 1:
        raise OutcomeUnknown()
    return ids[0]


def create_contact(client, contact_input: dict) -> str:
    """Create ONE contact (never batched). Returns its id. Same raises."""
    ids = _send(client, CREATE_CONTACT_QUERY, CREATE_CONTACT_OP,
                {"contacts": [contact_input]}, "contact", "contacts")
    if len(ids) != 1:
        raise OutcomeUnknown()
    return ids[0]
