"""The ONLY Famly writes the onboarding pipeline makes: create a child, create a contact, and
add RELATION = Parent to a contact that has none (`add_contact_relation`).

There is no delete or batch path in this module. The create documents are checked on every send
(`_assert_create_only`) to contain a `create(` call and nothing that deletes or updates; the one
update document is checked (`_assert_relation_update_only`) to be the single-field `records`
update and nothing else (never the full `contacts:` update, which can change name, phones and
roles).

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
UPDATE_RELATION_QUERY = HERE / "update_contact_relation.graphql"
UPDATE_RELATION_OP = "OnboardContactRelationUpdate"
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


def _assert_relation_update_only(path: Path) -> None:
    text = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ).lower()
    if ("update(records:" not in text or "update(contacts" in text or "create(" in text
            or "delete" in text):
        raise AssertionError(f"{path.name} is not the single-field records update")


def _rows(body, key: str, verb: str = "create") -> list[str]:
    """The created rows' ids in a response body (a dict or JSON text), or []."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return []
    try:
        rows = body["data"][key][verb]
        return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]
    except (KeyError, TypeError, IndexError):
        return []


def _send(client, path: Path, operation: str, variables: dict, kind: str, key: str,
          verb: str = "create"):
    """One request. Returns the created rows' ids.

    AN HTTP ERROR OR `errors` IN THE BODY DOES NOT MEAN THE MUTATION DID NOT RUN. A
    response that carries created rows is a creation, whatever else it carries. One
    that carries none is `CreateRejected` (a 4xx, or errors) or `OutcomeUnknown` (5xx,
    timeout, malformed); the CALLER settles either by a live lookup before it reports a
    failure. Nothing here ever sends twice."""
    (_assert_create_only if verb == "create" else _assert_relation_update_only)(path)
    try:
        body = client.execute(query_path=path, variables=variables, operation_name=operation)
    except GraphQLHTTPError as exc:
        ids = _rows(exc.body, key, verb)
        if ids:
            return ids
        if exc.status_code >= 500:
            raise OutcomeUnknown() from None
        raise CreateRejected(f"{kind}_http_{exc.status_code}") from None
    except GraphQLError as exc:
        ids = _rows(getattr(exc, "body", None), key, verb)
        if ids:
            return ids
        raise CreateRejected(f"{kind}_graphql_error") from None
    except (requests.RequestException, TimeoutError, ConnectionError):
        raise OutcomeUnknown() from None

    ids = _rows(body, key, verb)
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


def add_contact_relation(client, contact_id: str, value: str = "Parent") -> None:
    """Set the RELATION record of ONE contact through the single-field `records` argument, which
    MERGES per key (the contact's other records, name, phones and roles are untouched). Sent once;
    the caller only calls it for a contact whose RELATION it has just read as missing. Same raises
    as a create: the caller settles either by reading the contact's records."""
    ids = _send(
        client, UPDATE_RELATION_QUERY, UPDATE_RELATION_OP,
        {"records": [{"contactId": contact_id, "record": [{"key": "RELATION", "value": value}]}]},
        "relation", "contacts", verb="update")
    if ids != [str(contact_id)]:
        raise OutcomeUnknown()
