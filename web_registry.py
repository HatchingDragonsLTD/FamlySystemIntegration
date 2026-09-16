"""Registry of web actions, mirroring the CLI's ACTIONS list in main.py.

`server.py` is a router: it authenticates, reads the `action` field, and looks
the handler up here. It never imports an action module itself, so it does not
grow per-action logic.

FUTURE ACTIONS PLUG IN HERE. To add one:

    1. create `actions/<name>/web.py` exposing:
         ACTION_NAME = "<name>"
         handle(payload: dict) -> tuple[dict, int]
    2. import it below and add one entry to ACTIONS.

Nothing in server.py changes. The handler returns its own data dict and HTTP
status; the router wraps it in the standard envelope and classifies any
exception it raises.
"""

from actions.plan_write import approval as plan_write_approval
from actions.plan_write import web as plan_write_web

# One line per action. ACTION_NAME -> handler.
ACTIONS = {
    plan_write_web.ACTION_NAME: plan_write_web.handle,
}

# Slack button clicks. The router verifies the signature and identifies the
# action; the decision -- and the only write path to Famly -- lives in the
# action, so server.py stays free of per-action logic.
SLACK_CLICK_HANDLER = plan_write_approval.handle_click


def get_handler(action: str):
    """The handler for `action`, or None when it is not registered."""
    return ACTIONS.get(action)


def handle_slack_click(action_id: str, preview_id, user) -> str:
    """Act on a verified Slack button click; returns the message text."""
    return SLACK_CLICK_HANDLER(action_id, preview_id, user)


def action_names() -> list[str]:
    """Registered action names, sorted -- used in the 400 for a bad action."""
    return sorted(ACTIONS)
