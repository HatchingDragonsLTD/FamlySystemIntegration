# integrations/

Outbound/inbound bridges to other systems.

- `slack.py` -- posts plan previews for approval and verifies the button
  clicks Slack sends back. The buttons are **inert**: a click is verified,
  parsed and logged, and nothing is committed.

## Rules for this folder

- **No automated mutation triggers until the read-only query path is proven
  stable.** Everything in this project today is read-only: it previews plans and
  reports on them. Until that path has been exercised against real data and
  shown to be reliable, nothing here should write to Famly, HubSpot, or any
  other system. The Slack approval buttons exist so that the approval SIGNAL is
  proven -- signature verification included -- before any write is wired to it.
- Integration code should import runners from `actions/` directly
  (e.g. `from actions.staff_credentials.runner import run`). Runners return
  plain dataclasses and know nothing about the CLI, so they can be called from a
  server, a scheduler, or a queue worker without modification.
- Keep credentials for any integration in environment variables, the same way
  `core/config.py` handles the Famly token.
