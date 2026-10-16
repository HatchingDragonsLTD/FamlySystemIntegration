"""Compare the internal child summary (GET /api/v2/children/{id}/summary) with what
was sent, and return FLAGS. A flag is a code for a human to look at: it never
stops or holds a job, and it is never a reason to refuse a write.

THE SUMMARY'S EXACT SHAPE IS NOT DOCUMENTED here (the design doc says it holds the
child with start and end dates, isTestChild and group). The lookups below are
tolerant and listed in the constants. A field that cannot be found is the flag
`summary_unrecognised` (once), not a failure; only KEY NAMES are ever logged about
an unrecognised summary, never values.

What is compared, and the flag when it differs:
  * the group                                  `summary_group_mismatch`
  * the start date                             `summary_start_mismatch`
  * the end date (the one sent as lastDay)     `summary_end_mismatch`
  * isTestChild (true for a test deal, not
    true otherwise)                            `test_flag_mismatch`

NOT checked: the child's room move. (The first probe saw a room move start on the
creation date; that was fixed in Famly and is no longer something this pipeline
reads or stops on.) NOT checked either: gender, which the summary reports as a
Famly default when unset.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

GROUP_ID_KEYS = ("groupId", "currentGroupId")
GROUP_OBJECT_KEYS = ("group", "currentGroup")
START_KEYS = ("startDate", "start_date", "firstDay")
END_KEYS = ("endDate", "end_date", "lastDay")

UNRECOGNISED = "summary_unrecognised"
UNREADABLE = "summary_unreadable"
GROUP_MISMATCH = "summary_group_mismatch"
START_MISMATCH = "summary_start_mismatch"
END_MISMATCH = "summary_end_mismatch"
TEST_FLAG_MISMATCH = "test_flag_mismatch"


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _first(node: Any, keys) -> Any:
    """The first non-null value under any of `keys`, searching the document in
    dict order."""
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


def is_test_child(summary: Any) -> bool:
    values = [item["isTestChild"] for item in _walk(summary) if "isTestChild" in item]
    return bool(values) and all(v is True for v in values)


def _key_names(summary: Any) -> list[str]:
    names = set()
    for item in _walk(summary):
        names.update(item.keys())
    return sorted(names)


def flags(
    summary: Any,
    *,
    expected_group_id: str,
    expected_start: str,
    expected_end: str | None,
    expected_test: bool,
) -> list[str]:
    """The flags for this summary: an empty list if everything matches.

    Dates are compared as `YYYY-MM-DD`. Never raises.
    """
    if not isinstance(summary, (dict, list)):
        return [UNRECOGNISED]

    child = _child_node(summary)
    group = _group_id(summary)
    start = _date_part(_first(child, START_KEYS))
    end = _date_part(_first(child, END_KEYS))

    found: list[str] = []
    if group is None or start is None:
        # Key names only, never values: enough to learn the shape on a first run.
        logger.warning(
            "onboard: child summary shape not recognised (group=%s start=%s); keys seen: %s",
            group is not None, start is not None, _key_names(summary),
        )
        found.append(UNRECOGNISED)

    if group is not None and group != expected_group_id:
        found.append(GROUP_MISMATCH)
    if start is not None and start != expected_start:
        found.append(START_MISMATCH)
    if start is not None and (end or None) != (expected_end or None):
        found.append(END_MISMATCH)
    if is_test_child(summary) != bool(expected_test):
        found.append(TEST_FLAG_MISMATCH)
    return found
