"""Check the internal child summary (GET /api/v2/children/{id}/summary) after a
child is created.

THE SUMMARY'S EXACT SHAPE IS NOT DOCUMENTED here (the design doc says it holds
the child with start and end dates, isTestChild, group and onApp). So the
lookups below are tolerant and listed in the constants, and the check FAILS
CLOSED: a field it cannot find is `summary_unrecognised`, never a pass. Only
KEY NAMES are ever logged about an unrecognised summary, never values.

What must be true:
  * the group is the expected group;
  * the start date and end date are the expected ones (no end date when none was
    sent);
  * isTestChild is true for a test deal and NOT true otherwise;
  * the child's earliest room move STARTS ON THE CHILD'S START DATE, in the
    expected group. (A room move that starts on the creation date, as the first
    probe saw, is `room_move_mismatch`.)

Gender is deliberately not checked: the summary reports a Famly default for an
unset gender.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

GROUP_ID_KEYS = ("groupId", "currentGroupId")
GROUP_OBJECT_KEYS = ("group", "currentGroup")
START_KEYS = ("startDate", "start_date", "firstDay")
END_KEYS = ("endDate", "end_date", "lastDay")
MOVE_LIST_KEYS = ("groupMoves", "groups", "roomMoves", "moves", "groupHistory", "groupMovements")
MOVE_DATE_KEYS = ("startDate", "date", "from", "startsAt", "moveDate", "start")
MOVE_GROUP_KEYS = ("groupId", "id", "roomId")

OK = None
UNRECOGNISED = "summary_unrecognised"
GROUP_MISMATCH = "summary_group_mismatch"
START_MISMATCH = "summary_start_mismatch"
END_MISMATCH = "summary_end_mismatch"
TEST_FLAG_MISMATCH = "test_flag_mismatch"
ROOM_MOVE_MISMATCH = "room_move_mismatch"


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _first(node: Any, keys) -> Any:
    """The first non-null value under any of `keys`, searching the document
    breadth-first in dict order."""
    for item in _walk(node):
        for key in keys:
            if item.get(key) is not None:
                return item[key]
    return None


def _date_part(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _child_node(summary: Any) -> Any:
    if isinstance(summary, dict) and isinstance(summary.get("child"), dict):
        return summary["child"]
    return summary


def _group_id(summary: Any) -> str | None:
    child = _child_node(summary)
    for node in (child, summary):
        if not isinstance(node, dict):
            continue
        for key in GROUP_ID_KEYS:
            if node.get(key):
                return str(node[key])
        for key in GROUP_OBJECT_KEYS:
            obj = node.get(key)
            if isinstance(obj, dict) and obj.get("id"):
                return str(obj["id"])
    return None


def _moves(summary: Any) -> list[dict]:
    for item in _walk(summary):
        for key in MOVE_LIST_KEYS:
            value = item.get(key)
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                return value
    return []


def _move_date(move: dict) -> str | None:
    for key in MOVE_DATE_KEYS:
        if move.get(key):
            return _date_part(move[key])
    return None


def _move_group(move: dict) -> str | None:
    for key in MOVE_GROUP_KEYS:
        value = move.get(key)
        if isinstance(value, dict):
            value = value.get("id")
        if value:
            return str(value)
    return None


def is_test_child(summary: Any) -> bool:
    values = [item["isTestChild"] for item in _walk(summary) if "isTestChild" in item]
    return bool(values) and all(v is True for v in values)


def _key_names(summary: Any) -> list[str]:
    names = set()
    for item in _walk(summary):
        names.update(item.keys())
    return sorted(names)


def check(
    summary: Any,
    *,
    expected_group_id: str,
    expected_start: str,
    expected_end: str | None,
    expected_test: bool,
) -> str | None:
    """None if the summary matches, else the code of the first mismatch.

    Dates are compared as `YYYY-MM-DD`.
    """
    if not isinstance(summary, (dict, list)):
        return UNRECOGNISED

    group = _group_id(summary)
    start = _date_part(_first(_child_node(summary), START_KEYS))
    end = _date_part(_first(_child_node(summary), END_KEYS))
    moves = _moves(summary)

    if group is None or start is None or not moves:
        # Key names only, never values: enough to learn the shape on a first run.
        logger.warning(
            "onboard: child summary shape not recognised (group=%s start=%s moves=%s); "
            "keys seen: %s",
            group is not None, start is not None, bool(moves), _key_names(summary),
        )
        return UNRECOGNISED

    if group != expected_group_id:
        return GROUP_MISMATCH
    if start != expected_start:
        return START_MISMATCH
    if (end or None) != (expected_end or None):
        return END_MISMATCH
    if is_test_child(summary) != bool(expected_test):
        return TEST_FLAG_MISMATCH

    dated = [(_move_date(m), _move_group(m)) for m in moves]
    if any(d is None for d, _ in dated):
        return UNRECOGNISED
    earliest_date, earliest_group = min(dated, key=lambda item: item[0])
    if earliest_date != expected_start or (earliest_group and earliest_group != expected_group_id):
        return ROOM_MOVE_MISMATCH
    return OK
