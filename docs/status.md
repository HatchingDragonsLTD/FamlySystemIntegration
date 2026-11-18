# Status (current state, not a log)

Full history: docs/archive/status_to_2026-11-18.md. Design docs in docs/: famly_onboarding_pipeline_design, onboarding_bill_payer_design, onboarding_pipeline_state_and_checks, test_suite_report, handovers, reports_2026-10-27.
Add new entries as one or two lines. When this file passes 8 KB, move the oldest detail into the archive.

## Live / built (as of 2026-10-29 entries; confirm what is deployed under "To confirm")
- Onboarding pipeline (`actions/onboard`): HubSpot deal -> queue -> Slack Gate A approval -> Famly child, contacts and bill payer CREATES (never updates or deletes), then write-back to HubSpot. Webhook `POST /intake` action `onboard_enqueue`. End date always sent; HubSpot never blocks Famly; Retry button on hold notices.
- Plan write: `plan_write` (preview, Slack Approve commits via `runner.commit`), `queue_timeline` (unattended queue + `queue-worker`), `create_timeline` (chains). Server owns `famly_plan_status` / `famly_plan_note` (and legacy `famly_plan_uploaded`); rejections carry stable sub-codes and post one Slack line; held queue jobs are flagged needs_attention.
- Quality check cron: `onboard-quality-check`, read-only, Mon (digest) and Thu, 07:30 London, Slack alerts on new issues, CSV in reports/.
- Reference refresh cron: `reference-refresh`, daily 03:10 London, needs REFERENCE_DIR, validates and backs up before replacing the five generated reference files.
- Test suite guard: tests blank credentials and refuse real network calls (`tests/__init__.py`, `tests/network_guard.py`).

## .env switches (names only; values live in .env)
- COMMIT_ENABLED (master switch for Approve commits; dev false) and COMMIT_ALLOWED_CHILD_IDS (allow-list, `*` = all).
- QUEUE_MODE off | dry_run | auto (unset = dry_run), QUEUE_ALLOWED_INSTITUTIONS.
- ONBOARD_MODE off | dry | live (default off; dev off), ONBOARD_ALLOWED_DEAL_IDS, ONBOARD_TEST_DEAL_IDS, ONBOARD_ENFORCE_ALLOWLIST (only `off` disables).
- ONBOARD_BILL_PAYER on | off (default off), ONBOARD_BILL_PAYER_DETAIL_SELECTION, ONBOARD_URN_GENERATION on | off (default off; planned on at cutover 1 Dec 2026).
- PLAN_FLAG_LEGACY_WRITE (default on; `off` stops writing famly_plan_uploaded), ONBOARD_SCOPE_LEAVE_AFTER (default 2026-01-01).
- REFERENCE_DIR (outside the repo, shared by web, workers and cron), PREVIEW_STORE_PATH, ONBOARD_STORE_PATH.
- Secrets (never print): FAMLY_ACCESS_TOKEN, HUBSPOT_TOKEN, SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, INTAKE_SHARED_SECRET.

## Open work and parked
- HubSpot workflow: add the deal record id to the plan_preview webhook body and remove the step that sets famly_plan_uploaded (no flag is written until the id is sent).
- HubSpot dropdowns: `child_linked` and `bill_payer_linked` on famly_sync_status must be offered, or the status stays at the earlier value.
- Parked: adding a HUBSPOT reference to an EXISTING child (needs a full children.update echo; not built); adding a reference to an existing contact (full contacts update; not built); parent app roles and logins; Shoreditch URN site code.
- Delete when done: temporary `urn_lookup` and `delete_plans` actions; probe commands (`probe-bill-payers`, `probe-child-create`, `probe-contact-create`) once no longer needed.

## Commands used most
- `python main.py onboard-enqueue --deal-id ID [--reopen-done]` queue a deal (`--file PATH --limit N|--all` for a batch).
- `python main.py onboard-dry-run --deal-id ID` plan only, no writes.
- `python main.py onboard-worker [--once]` run the onboarding worker.
- `python main.py onboard-audit --operations [--out CSV]` read-only audit of onboarded deals.
- `python main.py onboard-quality-check [--baseline|--digest]` the cron job.
- `python main.py queue-worker [--once]`, `queue-status`, `queue-list`, `queue-held`, `queue-pause`, `queue-resume`, `queue-cancel`.
- `python main.py plan-check --file ids.txt` read-only check of deals against the plan rules.
- `python main.py reference-refresh [--dry-run]` and `pull-references` refresh reference/ JSON.
- `python main.py hubspot-set-plan-status --from-famly (--operations|--file) [--confirm]` backfill plan status (dry run by default).
- `python main.py chain-status|chain-resume|chain-cancel|chain-sweep` manage create_timeline chains.

## To confirm
- Which values the production .env actually holds (COMMIT_ENABLED, COMMIT_ALLOWED_CHILD_IDS, QUEUE_MODE, ONBOARD_MODE, ONBOARD_BILL_PAYER, ONBOARD_URN_GENERATION, PLAN_FLAG_LEGACY_WRITE); the archive records only the laptop (COMMIT_ENABLED true, allow-list `*`) and never the droplet.
- Whether the 2026-10-28/29 work (plan status, rejection sub-codes, held-queue visibility, reference-refresh and its cron, pricing-input guards) has been deployed and exercised live; those entries say "fakes only, NOT run live".
- Whether the quality-check cron runs at 07:30 London or UTC on the droplet (cron may ignore CRON_TZ; switch to 06:30 on 2027-03-28 if UTC).
- Open in the archive: WhatsApp property name, child-name deal properties, month-end group boundary rule, `var/urns_missing.txt`, sibling deals sharing one Bill Payer label.
