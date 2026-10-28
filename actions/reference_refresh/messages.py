"""Slack wording for reference-refresh. Ids, codes and counts only: no names, no values."""

MAX_IDS = 5


def _ids(ids) -> str:
    ids = list(ids)
    shown = ", ".join(ids[:MAX_IDS])
    return f"{shown} and {len(ids) - MAX_IDS} more" if len(ids) > MAX_IDS else shown


def stopped(problems: dict[str, list[str]]) -> str:
    """A pull reported an error: nothing was replaced. problems: pull name -> institution codes or codes."""
    lines = ["Reference refresh STOPPED: a pull reported an error, so nothing was replaced."]
    for pull, items in sorted(problems.items()):
        lines.append(f"• {pull}: {_ids(items)}")
    return "\n".join(lines)


def refused(refusals: list[dict]) -> str:
    """A staged file failed validation: every live file was kept. Each: name, old, new, reason."""
    lines = ["Reference refresh REFUSED: every live file was kept."]
    for item in refusals:
        lines.append(f"• {item['file']}: {item['reason']} (old {item['old']} entries, new {item['new']})")
    return "\n".join(lines)


def replace_failed(file: str, restored: int) -> str:
    return (f"Reference refresh FAILED while replacing {file}: the live files were restored "
            f"({restored} already replaced file(s) put back from their backups).")


def summary(changes: dict[str, dict[str, dict[str, list[str]]]], pricing: dict[str, dict[str, list[str]]]) -> str:
    """After a replace. changes: file -> institution -> {added, removed, changed: [ids]}.
    pricing: institution -> {changed keys}. Counts and ids only."""
    lines = [f"Reference data refreshed: {len(changes)} file(s) changed."]
    for file, institutions in sorted(changes.items()):
        for institution, kinds in sorted(institutions.items()):
            counts = ", ".join(f"{len(kinds[k])} {k}" for k in ("added", "removed", "changed"))
            ids = [i for k in ("added", "removed", "changed") for i in kinds[k]]
            lines.append(f"• {file} {institution}: {counts}" + (f" ({_ids(ids)})" if ids else ""))
    if pricing:
        total = sum(len(v) for v in pricing.values())
        lines.append(f":warning: pricing inputs changed (institution_defaults): {total} key(s)")
        for institution, keys in sorted(pricing.items()):
            lines.append(f"  - {institution}: {len(keys)} key(s): {_ids(sorted(keys))}")
    return "\n".join(lines)
