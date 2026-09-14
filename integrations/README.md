# integrations/

Reserved for a future HubSpot / webhook bridge layer.

Nothing is implemented here yet, and that is deliberate.

## Rules for this folder

- **No automated mutation triggers until the read-only query path is proven
  stable.** Everything in this project today is read-only: it runs a GraphQL
  query and prints the result. Until that path has been exercised against real
  data and shown to be reliable, nothing here should write to Famly, HubSpot, or
  any other system.
- Integration code should import runners from `actions/` directly
  (e.g. `from actions.staff_credentials.runner import run`). Runners return
  plain dataclasses and know nothing about the CLI, so they can be called from a
  server, a scheduler, or a queue worker without modification.
- Keep credentials for any integration in environment variables, the same way
  `core/config.py` handles the Famly token.
