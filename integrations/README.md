# integrations/

Outbound/inbound bridges to other systems.

- `slack.py` -- posts plan previews for approval and verifies the button
  clicks Slack sends back. **Approve now commits a plan to Famly**, behind the
  guards in `actions/plan_write/approval.py`; Reject records the decision and
  writes nothing.
- `catalogue.py` -- local UUID -> name map for labelling the preview summary.

## Rules for this folder

- **No automated mutation triggers until the read-only query path is proven
  stable.** Everything in this project today is read-only: it previews plans and
  reports on them. Until that path has been exercised against real data and
  shown to be reliable, nothing here should write to Famly, HubSpot, or any
  other system. That rule has now been lifted for ONE operation only: creating
  a plan from a Slack approval. It is gated by COMMIT_ENABLED (default false)
  and an explicit allow-list of child IDs. Overwriting a plan and adding a
  second plan remain unbuilt.
- Integration code should import runners from `actions/` directly
  (e.g. `from actions.staff_credentials.runner import run`). Runners return
  plain dataclasses and know nothing about the CLI, so they can be called from a
  server, a scheduler, or a queue worker without modification.
- Keep credentials for any integration in environment variables, the same way
  `core/config.py` handles the Famly token.
