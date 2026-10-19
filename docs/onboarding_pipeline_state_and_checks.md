# Onboarding pipeline: what is built, and every check in the flow

Status as of 2026-10-16. Based on the design doc (sections 1 to 18) and Claude Code's summaries. Nothing here has run against live Famly or HubSpot yet. At the last report, the phase 2 code, the HTTP trigger and the history-based origin rule were built and tested with fakes, and the full test suite was still running, so nothing was committed.

## 1. What this does

When a deal reaches Deposit Pending in HubSpot's Operations pipeline, the server creates the child and the parent contacts in Famly, then writes the result back to the deal. Everything else (bill payer, plans, deposit, invoice, contract, app access, siblings) is a later phase and is not built.

## 2. The flow, step by step, with every check

### A. HubSpot workflow (built by Drew, not yet switched on)
- Trigger: Operations deal enters Deposit Pending. Re-enrolment off.
- Planned enrolment criteria: child first and last name, date of birth, start date and location filled in. Planned delay of a few minutes, because the Operations deal is copied from the Sales deal and may not be complete at creation.
- Action: "Send a webhook", POST to the intake URL with header X-Intake-Secret, body `{"action": "onboard_enqueue", "dealId": <record id>}`. Drew still needs to add the static value `action` = `onboard_enqueue`.
- Keep the workflow OFF until the server is in dry mode. In off mode the server answers 200 and creates no job, so a deal that enrols early is silently dropped and is not retried.

### B. Endpoint (existing /intake route, new action onboard_enqueue)
Checks, in order:
1. Secret compared with `hmac.compare_digest`. Wrong or missing: 401, no detail. (To confirm: a non-ASCII secret header should return 401, not 500.)
2. `dealId` must be a string or number of ASCII digits only. Anything else: 400, value never echoed.
3. It only enqueues. No Famly or HubSpot calls in the request. It returns the job id at once.
4. Response codes: 200 for queued, already_queued, already_done, refused_mode_off, refused_not_allowlisted. 500 only for genuine internal errors, with the exception type logged and its text never returned. Deliberate refusals return 200 so HubSpot does not retry them.
5. Live mode and deal not on the allowlist: no job, Slack notice with deal id and code only. Dry mode accepts any deal.
6. Jobs record `source` of `cli` or `webhook`.

### C. Queue and worker
- `onboard_jobs` is its own SQLite file. A unique partial index allows only one active job per deal (queued, awaiting approval, or mid-write). Held, needs_review, failed and done jobs do not block a new job, except that a done deal creates no job (`already_done`).
- Worker is `main.py onboard-worker` (a poll loop). A job found mid-write after a crash becomes needs_review (`interrupted`) and is never replayed.
- Modes: `ONBOARD_MODE=off|dry|live`, default off. Dry makes no writes. Live writes only for deals in `ONBOARD_ALLOWED_DEAL_IDS`. `isTestChild` is set only for deals in both the allowlist and `ONBOARD_TEST_DEAL_IDS`.
- Nothing runs the worker on the droplet yet. A systemd unit is needed (see section 6).

### D. Plan, re-run live on every job (nothing from the roster cache)
Webhook jobs first confirm the deal is in Operations at Deposit Pending, else HOLD `stage_not_eligible` (no Famly read, no HubSpot write). CLI jobs skip this check.

Holds (no writes, Slack notice):
- `site_unmapped`: location_of_interest other than Westminster, City or Canada Water (Shoreditch, Head office, Nannying, Other, empty).
- `group_missing`: no group for that site (Shoreditch has none).
- Missing date of birth or start date.
- `child_ambiguous`: two or more Famly children match.
- `bill_payer_unclear`: not exactly one contact with the Bill Payer label.
- `contact_no_email`.
- `child_name_incomplete` and `dob_missing` (only when a child would be created).
- `end_date` before start date. Left Nursery with empty `end_date`.
- `contact_unmatched_on_existing_child` (see the origin rule below).

Derivations:
- Site from `location_of_interest`. Group from age in completed months at the START date: under 24 Hatchlings, 24 to under 36 Littlewings, 36 or more Voyagers. Drew said children who already left go in Voyagers. CONFIRM Claude Code implemented that exception, because the phase 1 prompt only described age at start.
- Dates: sitesRelation firstDay = `actual_start_date_1`, lastDay = `end_date` when set. A past lastDay makes the child inactive, hidden from current and upcoming lists, and excluded from counts.

Child lookup order: public `listByExternalSystemReference` with HUBSPOT = deal id; if nothing, the live URN search. Exactly one is found, none is create, two or more is HOLD.

Child origin (decides what happens to unmatched contacts):
- `created_by_pipeline` only if an earlier job for the same deal recorded sending a create that produced this child id. Unmatched contacts are then `would_create`, so a re-run after a partial failure finishes the job.
- `existing` for everything else, including a child found by URN fallback, one found by deal id with no job record, and an id recorded for a different deal. Unmatched contacts then HOLD.
- Email comparison: trim, lowercase, googlemail.com equals gmail.com. Dots and +tags are different.
- Consequence: the onboard jobs database is part of the safety logic. Do not delete it, include it in backups, and do not run test jobs from the laptop against the same deals.

### E. Gate A (Slack approval)
- Required for any live plan that would write anything (child or contacts). A plan with nothing to write needs none.
- The message shows deal id, a HubSpot link, site, group, counts of writes, child state, and codes. No names.
- The button carries job id and plan hash. If the plan changes before the click, the old message is invalidated and approval is asked again. After a click the plan is re-run first.
- Known limit: the hash covers presence flags and counts, not email text, so an email edit between approval and write does not force re-approval.
- The Slack click is routed by `server.py` and `web_registry.py`, so it must reach the same machine and database as the worker (the droplet).

### F. Child creation
1. Immediately before writing, look the child up again. If one now exists, do not create.
2. Input is the probed shape: name as "First Last" string, birthday %Y-%m-%d, group, empty records and sensitiveRecords, gender omitted, sitesRelation, references HUBSPOT (deal id) and URN. No nationality, language, birthplace or extra info.
3. The create is sent once and persisted so a job cannot send it twice. HTTP 400 "Transient" is never retried. An unknown outcome (timeout, 5xx) is settled by a live lookup on the deal id.
4. The child id is saved the moment the create succeeds, not after read-back.
5. Read-back: exactly one child by deal id; the internal summary shows the expected group, start date and end date; and the current room move starts on the child's start date. Any mismatch stops everything (`room_move_mismatch` for the last), with no further writes. If the summary shape is not recognised the job fails closed (`summary_unrecognised`).
6. Gender "Dreng" in the summary is treated as a Famly display default and is not checked.

### G. Contacts, one create call per contact
1. Live check first: the child's contacts by canonical email. A match is `present`, skip.
2. Missing first or last name: that contact is held (`contact_name_incomplete`), the others continue.
3. Input: name string, canonical email, the one childId, `roles: []` (no login, no invitation), records RELATION = Parent, `emergencyContact` true for the bill payer only.
4. Phone: mobilephone, then phone, then hs_whatsapp_phone_number, then none. Normalised to +44 (leading 0 becomes +44, spaces and punctuation removed). If it cannot be normalised confidently, omit it and warn `phone_unnormalised`.
5. Address only if street, zip and city are all present. Otherwise omit it and warn `address_incomplete`. Country sent as given, never defaulted.
6. Read-back per contact: appears exactly once with the expected email. A rejected or unlanded create stops the remaining contacts and the job ends needs_review. The child is never rolled back.

### H. HubSpot write-back (live only)
- Before any Famly write, list the deal properties and fail closed if any of the four are missing: `famly_childid`, `famly_sync_status`, `famly_sync_note`, `famly_sync_gaps`. Status must accept `child_ready`, `contacts_linked`, `needs_attention` as internal values. `famly_sync_note` and `famly_sync_gaps` are plain multi-line text (the job writes plain-English sentences, no codes).
- Note and gaps hold codes only.
- A write-back failure after Famly writes is needs_review (`hubspot_writeback_failed`) and never causes a Famly retry. The write-back alone is idempotent.

### I. Re-runs
- A done deal creates no new job. A held, needs_review or failed deal starts a fresh job whose first step is the live plan, which sees the child and contacts already present and does only what is missing.

## 3. Data handling rules
Names, emails, phones, addresses and dates of birth are read from HubSpot at run time, held in memory for one call, and never stored, logged, put in Slack, put in the job table, or put in test expectations. HubSpot reads use an explicit property allow-list. `HubSpotClient` is GET only; the only write path is `HubSpotDealWriter`, limited to the four properties.

## 4. Decisions made
- Child first, then contacts, always created from HubSpot with empty roles. The copy-contacts feature is dropped.
- All associated contacts are parents for now. The risk (a non-parent associated contact) is deferred to the later app-access pipeline.
- Bill payer is the contact carrying the Bill Payer association label (one per deal; a contact can be bill payer on many deals).
- Gender omitted. No sensitive records. Leaving reason defaults to "Other" and is accepted.
- Contacts on an existing (legacy) child never get unmatched contacts auto-created, because the migrated Famly data may have different emails.

## 5. Known unverified (a live test settles these)
- Whether Famly splits "First Last" correctly, especially two-word first names and double-barrelled surnames, for both child and contacts.
- Stored value of RELATION, and that roles are really empty (no login).
- Whether Drew's updated room move logic works (the read-back checks it).
- Whether `famly_sync_status` accepts the three values.
- Whether the lookup still finds soft-deleted children, which affects re-testing the same test deal.
- Non-ASCII secret header behaviour.

## 6. Deployment and first live test
1. Commit locally once the full suite is clean (single commit is fine), including the fix to the stale origin lines in `docs/status.md` (lines 5 and 6).
2. On the droplet, read-only: confirm the unit names with `systemctl list-unit-files` and `systemctl cat famly-queue-worker`, and the env variable names with the redacted `.env` listing.
3. Push, pull in `/home/drew/famly`, set `ONBOARD_MODE=off`, the allowlist and test-deal variables (the test deal only), and `HUBSPOT_TOKEN` in `.env`.
4. Create the `famly-onboard-worker` systemd unit (same env file, `ExecStart=.../python main.py onboard-worker`, `Restart=always`), enable it.
5. Restart gunicorn (the route loads at start-up), start the onboarding worker, restart `famly-queue-worker` so both run the same code.
6. Check the endpoint with curl: wrong secret returns 401, correct secret in off mode returns 200 `refused_mode_off`.
7. Check the four `famly_*` properties exist in HubSpot and the status values are exact.
8. Create a test deal in Operations at Deposit Pending: test child with an awkward name, date of birth, future start, Canada Water, one associated contact with the Bill Payer label (name, phone, address, test email).
9. Switch to dry, curl the test deal id in, read the plan and Slack message.
10. Switch to live, enqueue, approve in Slack, let the worker run.
11. Check Famly: child exists, isTestChild, dates, group, room move start, name splits, relation, phone, address, no login. Check HubSpot: four properties written.
12. Enqueue the same deal again: expect `already_done` and nothing created.
13. Clean up: `probe-child-delete` for the test children, contacts by hand in Famly.
14. Only then switch the HubSpot workflow on, with manual enrolment of the test deal first.

## 7. Housekeeping still open
- Delete the two probe children (`3388ee6a-0320-4015-9bee-f744fbb0b292`, `e274a615-2b63-480d-9a8f-129acc072668`) with `probe-child-delete --confirm`, and the four AAProbe contacts by hand.
- Sync the design doc into the repo's `docs/` folder (the repo copy lacks sections 12 to 18). Add a section recording the child origin rule, which overrides section 16.
- Add a Slack retry button on hold notices later, so a held deal can be re-run after data is fixed.

## 8. Not built
Bill payer (existence check by email, addChildren), plans via the existing plan queue, deposit (unpaid at Deposit Pending, marked paid for later stages, none for Left Nursery, idempotency check first), invoice, contract upload from the signed HubSpot quote, sibling linking, backfill batching with batch-level approval, app access (Parent role after deposit paid, with a label filter for non-parents).
