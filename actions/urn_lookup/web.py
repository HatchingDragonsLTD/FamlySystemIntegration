"""Web handler for the urn_lookup action -- TEMPORARY BACKFILL UTILITY.

See `runner.py`'s module docstring for what this is and when to delete it.
Read-only: this endpoint never writes to Famly or HubSpot. It resolves a
HubSpot deal's `urn` to a Famly childId by matching against Famly's cached
child roster; a HubSpot workflow step is what actually writes the resolved id
back into the deal property, the same way every other action's response is
consumed.

Request body:

    {"action": "urn_lookup", "urn": "..."}

Response, via the standard envelope (see server.py):

    exactly one match -> ok=true,  data={"childId": "...", "matched": true,
                          "childName": "..."} (the name is for the HubSpot
                          operator's own sanity-checking, not for any logic)
    zero matches      -> ok=true,  data={"childId": null, "matched": false}
                          -- a legitimate "not found yet" result, not an
                          error; the calling workflow can branch on it
    2+ matches        -> ok=false, data={"childId": null, "matched": false,
                          "matchCount": N}, errors naming every id -- a real
                          data problem in Famly (a duplicate externalId),
                          never guessed at

The router knows only this module's ACTION_NAME and `handle`.
"""

import logging

from . import runner

logger = logging.getLogger(__name__)

ACTION_NAME = "urn_lookup"


def handle(payload: dict) -> tuple[dict, int]:
    """Resolve `urn` against Famly's (cached) child roster.

    Args:
        payload: the request body. Only `urn` matters here.

    Returns:
        (data, status). 200 for both a match and a legitimate zero-match
        result -- both are facts this endpoint reports, not errors. 422 for a
        missing `urn` (nothing to look up). 409 for 2+ matches -- a Famly data
        problem this action cannot resolve on its own.

    Raises:
        GraphQLError / GraphQLHTTPError: propagated deliberately from the
            roster fetch, uncaught, so a Famly API failure (including one
            naming a missing/required argument) is surfaced clearly via the
            router's classification rather than silently producing a wrong or
            empty match.
    """
    urn = payload.get("urn") if isinstance(payload, dict) else None

    data = {
        "childId": None,
        "matched": False,
        "warnings": [],
        "errors": [],
    }

    if not isinstance(urn, str) or not urn.strip():
        data["errors"] = ["missing 'urn'"]
        return data, 422

    roster = runner.get_roster()
    matches = runner.find_matches(urn, roster)

    if len(matches) == 1:
        match = matches[0]
        data["childId"] = match.id
        data["matched"] = True
        data["childName"] = match.full_name
        return data, 200

    if not matches:
        return data, 200

    # 2+: a real data problem (a duplicate externalId in Famly). No guessing
    # which one is right -- that decision belongs to a human.
    ids = [match.id for match in matches]
    data["matchCount"] = len(matches)
    data["errors"] = [
        f"Multiple children share externalId {urn!r}: {', '.join(str(i) for i in ids)}"
    ]
    logger.error("urn_lookup: multiple children share externalId %r: %s", urn, ids)
    return data, 409
