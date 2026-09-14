"""Staff credentials action.

Owns its GraphQL query file (query.graphql) and its own response types.
Importable directly by a future server or scheduler: call `run(employee_ids)`
and you get parsed objects back -- no CLI involvement.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.client import GraphQLClient

QUERY_PATH = Path(__file__).with_name("query.graphql")

# Must match the operation name declared inside query.graphql.
OPERATION_NAME = "GetStaffCredentialAssignments"

# Union/interface members of `assignments` that we treat specially.
TYPENAME_LEVEL = "Level"
TYPENAME_BADGE = "Badge"


@dataclass
class Qualification:
    id: str | None = None
    name: str | None = None


@dataclass
class QualificationFile:
    """A file attached to an assignment.

    NOTE: `url` is a pre-signed S3 link that expires roughly two hours after the
    request. Download it in the same run; do not store it for later.
    """

    id: str | None = None
    name: str | None = None
    url: str | None = None


@dataclass
class Assignment:
    """One item from `staffQualifications.assignments`.

    The field is a GraphQL union: each item carries a `__typename` which may be
    `Level`, `Badge`, or a plain qualification type. Only `Level` items carry a
    `level` field, so it is Optional here and must never be assumed present.
    The untouched item is kept in `raw` so fields we do not model are still
    reachable.
    """

    typename: str | None = None
    id: str | None = None
    title: str | None = None
    qualification_date: str | None = None
    expiration_date: str | None = None
    note: str | None = None
    certificate_number: str | None = None
    qualification: Qualification | None = None
    files: list[QualificationFile] = field(default_factory=list)
    level: Any | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_level(self) -> bool:
        return self.typename == TYPENAME_LEVEL

    @property
    def is_badge(self) -> bool:
        return self.typename == TYPENAME_BADGE

    @property
    def qualification_name(self) -> str | None:
        return self.qualification.name if self.qualification else None

    def to_summary(self) -> dict:
        """The flattened shape (mirrors the fields picked out in Postman)."""
        return {
            "title": self.title,
            "qualificationDate": self.qualification_date,
            "note": self.note,
            "certificateNumber": self.certificate_number,
            "qualification": self.qualification_name,
        }


def _parse_qualification(node: Any) -> Qualification | None:
    if not isinstance(node, dict):
        return None
    return Qualification(id=node.get("id"), name=node.get("name"))


def _parse_files(node: Any) -> list[QualificationFile]:
    if not isinstance(node, list):
        return []
    return [
        QualificationFile(id=f.get("id"), name=f.get("name"), url=f.get("url"))
        for f in node
        if isinstance(f, dict)
    ]


def _parse_assignment(item: Any) -> Assignment:
    """Parse one union member, switching on `__typename`.

    Defensive by design: unknown typenames are kept rather than dropped, and
    `level` is only read for `Level` items -- and may be absent even there.
    """
    if not isinstance(item, dict):
        return Assignment(raw={"value": item})

    typename = item.get("__typename")

    assignment = Assignment(
        typename=typename,
        id=item.get("id"),
        title=item.get("title"),
        qualification_date=item.get("qualificationDate"),
        expiration_date=item.get("expirationDate"),
        note=item.get("note"),
        certificate_number=item.get("certificateNumber"),
        qualification=_parse_qualification(item.get("qualification")),
        files=_parse_files(item.get("files")),
        raw=item,
    )

    # Only this branch may look at `level`.
    if typename == TYPENAME_LEVEL:
        assignment.level = item.get("level")

    return assignment


def parse_response(body: dict) -> list[Assignment]:
    """Map a raw GraphQL response body into Assignment objects."""
    if not isinstance(body, dict):
        return []

    data = body.get("data") or {}
    staff_qualifications = data.get("staffQualifications") or {}
    assignments = staff_qualifications.get("assignments")

    if not isinstance(assignments, list):
        return []

    return [_parse_assignment(item) for item in assignments]


def summarise(assignments: list[Assignment]) -> list[dict]:
    """Flatten assignments to the Postman-style summary rows."""
    return [a.to_summary() for a in assignments]


def run(
    employee_ids: list[str],
    client: GraphQLClient | None = None,
) -> list[Assignment]:
    """Fetch and parse qualification assignments for the given employee IDs.

    Args:
        employee_ids: Famly employee IDs to query.
        client: Optional client, mainly for tests or for reuse across calls.

    Returns:
        The parsed Assignment objects, in the order the API returned them.
    """
    client = client or GraphQLClient()

    body = client.execute(
        query_path=QUERY_PATH,
        variables={"employeeIds": list(employee_ids)},
        operation_name=OPERATION_NAME,
    )

    return parse_response(body)
