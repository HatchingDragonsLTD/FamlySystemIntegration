# famly-tool

A small, read-only Python tool for querying Famly's internal GraphQL API for our
own childcare setting's internal use.

Structure modelled on [jacobbunk/famly-fetch](https://github.com/jacobbunk/famly-fetch):
GraphQL query text lives in `.graphql` files, separate from the code that sends
requests; configuration and the access token come from the environment.

## Layout

```
core/config.py                         settings from environment variables
core/session.py                        attaches the access token to requests
core/client.py                         generic GraphQL client (query-agnostic)
core/rest_client.py                    generic REST client (endpoint-agnostic)
actions/<name>/query.graphql           the query text (GraphQL actions only)
actions/read_child_plans/runner.py     reads a child's plans (REST)
actions/<name>/runner.py               does the work, owns its response types
actions/<name>/cli.py                  argparse wiring only
actions/plan_write/builder.py          builds the plan write request body
actions/plan_write/warnings.py         classifies + escalates plan warnings
actions/plan_write/input_schema.py     the normalized input contract
actions/plan_write/csv_loader.py       CSV -> normalized input
actions/plan_write/hubspot_intake.py   webhook payload -> normalized input
actions/plan_write/hubspot_flatten.py  flat HubSpot fields -> nested plan shape
actions/plan_write/web.py              plan_preview web handler
samples/plans.sample.csv               example CSV (the only committed one)
integrations/slack.py                  Slack post/update + signature check
tests/test_slack.py                    unit tests (stdlib unittest)
web_registry.py                        action name -> web handler
server.py                              thin HTTP router (no per-action logic)
main.py                                CLI dispatcher
```

The separation matters: `core/client.py` knows nothing about any specific query,
and each action owns its own query file and its own dataclasses. A future server
or scheduler can `from actions.staff_credentials.runner import run` and call it
directly — the CLI is just one caller.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Setting the token

Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
```

| Variable | Required | Notes |
| --- | --- | --- |
| `FAMLY_ACCESS_TOKEN` | yes | Your Famly access token. Sent as the `x-famly-accesstoken` header. |
| `FAMLY_GRAPHQL_URL` | no | Defaults to `https://app.famly.co/graphql`. Set this to your real endpoint. |

`.env` is gitignored. The token is never hardcoded anywhere in the source, and
real environment variables take precedence over `.env`.

There is no login flow. `core/session.py` only attaches a token you supply;
`TokenSession.refresh()` is a deliberate stub that raises `NotImplementedError`
and marks where a real refresh flow would go. If the token expires, put a new
one in `.env`.

## The query

`actions/staff_credentials/query.graphql` holds the operation
`GetStaffCredentialAssignments`, which takes `$employeeIds: [EmployeeId!]` and
returns `staffQualifications.assignments`.

Two things must stay in step with that file:

- the operation name must match `OPERATION_NAME` in
  `actions/staff_credentials/runner.py`;
- the variables the runner sends (`employeeIds`) must match the query.

They are the only coupling between the query and the code.

## Running

```bash
python main.py staff-credentials <employeeId> [<employeeId> ...]
```

Output is JSON. Exit codes: `0` success, `1` request or GraphQL error,
`2` configuration error.

### Output format

`--format summary` (the default) prints the flattened rows: `title`,
`qualificationDate`, `note`, `certificateNumber`, `qualification` — the same
selection as the Postman test script this replaced.

`--format full` prints every parsed field, including `expirationDate`, `level`
(on `Level` assignments only), and attached `files` with their URLs.

### Sorting

`--sort {title,date,expiry,qualification}` sorts the output; omit it to keep the
order the API returned. Assignments missing the sort field sort last.

```bash
python main.py staff-credentials abc123 --sort date
python main.py staff-credentials abc123 --format full --sort expiry
```

## Response shape

`assignments` comes back as one flat list covering all the employee IDs you
asked for — the assignments themselves do not carry an employee ID, so results
cannot be grouped per employee unless that field is added to the query.

Each item is a union member identified by `__typename`: `Level`, `Badge`, or a
plain qualification type. Only `Level` items carry a `level` field, and the
parser reads it only on that branch. Unrecognised typenames are kept, not
dropped, and every item retains its untouched response dict in `raw`.

### Reading a child's plans

```bash
python main.py read-child-plans <childId> [--version 3] [--format summary|full]
```

`GET {rest_base}/v2/plans/?version=&childId=&flexiblePackages=true`. The summary
gives what a create-vs-edit decision needs: `planId`, `version`, `from`/`to`,
`monthlyEstimate`, `planPartIds` and a session count, alongside `hasPlans` and
`currentPlanId`.

`currentPlanId` is populated only when the child has **exactly one** plan. With
zero or more than one it is null, so an ambiguous case has to be resolved
deliberately rather than by taking the first row.

The write path calls `runner.run()` directly for the same information.

### Adding another action

Create `actions/<name>/runner.py` with a `run(...)` function and an
`actions/<name>/cli.py` exposing NAME/HELP/add_args/handle, then add one import
and one entry to `ACTIONS` in `main.py`. GraphQL actions also get a
`query.graphql`; REST actions use `core/rest_client.py` instead.

## Writing plans (the `plan` action)

Plan create/update is **REST, not GraphQL**: `POST /api/v2/plans` with
`?preview=<bool>&version=<int>`, authenticated with the same
`x-famly-accesstoken` header via `TokenSession`.

### Preview vs commit

Preview and commit are the **same request**. The only difference is one query
param:

| | `preview=true` | `preview=false` |
| --- | --- | --- |
| Server calculates the plan | yes | yes |
| Anything persisted | **no** | **yes** |

That single flag is all that separates a dry run from a write, which is exactly
why `commit()` is guarded.

```bash
# Dry run -- safe, never writes
python main.py plan --from-json plan.json --version 3

# Write -- both extra flags are required
python main.py plan --from-json plan.json --version 3 --mode commit     --confirm --test-child <childId>
```

`--from-json` takes either the full `{"plan": {...}}` wrapper or a bare plan
object. Build one in Python with `actions/plan_write/builder.py`
(`build_plan_body`), which sends the sparse shape and generates `planPartId`
UUIDs; the server backfills prices, session versions and booking IDs.

### Guards on commit

`runner.commit()` refuses, before sending anything, unless:

1. `confirm=True` is passed explicitly, and
2. the body's `childId` appears in `allowed_child_ids` (the CLI fills this from
   `--test-child`, repeatable).

It then runs a **preview first**, surfaces the warnings, and only then posts
with `preview=false`. The guards live in the runner, not the CLI, so a server
importing `commit()` directly gets identical protection. A refusal exits `3`.

**The first real commit must target a disposable test child.** Nothing in the
code knows which child IDs are real, so this is enforced by you naming the test
child on every commit — there is no default allow-list and an empty one refuses.

### Warnings are the only failure signal

The endpoint returns **HTTP 200 even when the plan is invalid**. Validation
problems come back inside `behaviors[]` as the `ShowPlanWarnings` entry, so a
successful status code means nothing on its own.

Both modes therefore always extract warnings, print them prominently to stderr,
and return them on the result. `actions/plan_write/warnings.py` classifies each
one against `KNOWN_WARNINGS`; anything unrecognised is escalated through
`notify()`, which currently logs to stderr prefixed `UNHANDLED PLAN WARNING`.
Swap the delivery with `set_notifier(fn)` when an `integrations/` bridge exists
— callers do not change.

`KNOWN_WARNINGS` is seeded with the funding-mismatch case. Its `error` code is a
placeholder until a real warning payload is captured; the title match carries it
until then, and anything unmatched escalates rather than passing silently.

### The normalized input contract

Plans can be driven from a CSV file or from a HubSpot custom-code action. Both
produce **the same normalized shape**, defined as dataclasses in
`actions/plan_write/input_schema.py`. That shape is the contract; everything
downstream of it is shared.

```
CSV file ---------                   >--- PlanInput ---> validate() ---> to_plan_body() ---> builder ---> preview
HubSpot payload --/
```

**UUIDs only.** Every label-to-UUID resolution — billing profile names, session
names, product names, funding types — happens **upstream**, in whatever builds
the payload. This codebase performs no label lookup and should never learn how
to: a label that reaches `validate()` is reported as a non-UUID and rejected.

| Field | Notes |
| --- | --- |
| `childId` | UUID, required |
| `from` | ISO date `YYYY-MM-DD`, required |
| `ruleGroupId` | UUID or null |
| `note` | free text, defaults to `""` |
| `planParts[]` | at least one required |
| `planParts[].billingProfileId` | UUID, required |
| `planParts[].attendanceScheduleId` | UUID, required |
| `planParts[].billing` | `{id, title, weeksOfCare, invoices}`; `id` is a billing-scheme enum such as `ANNUALIZED_V2`, **not** a UUID |
| `planParts[].termScheduleId` | UUID or null |
| `planParts[].termTimeOnly` | boolean, defaults to `false` |
| `planParts[].sessionBookings[]` | `{sessionId (UUID), day, fundable (bool)}` |
| `planParts[].productBookings[]` | `{productId (UUID), day, amount (positive int)}` |
| `publicFundingSettings` | `{method, hours, minutes, fundingTypeIds[], maxFundedMinutes}` or null |

`day` must be one of `MONDAY`…`SUNDAY`. Keys may be camelCase or snake_case.

`billing.id` is the one id in the contract that is **not** a UUID — it names a
billing scheme. `validate` hard-fails it only when missing or empty; a value
outside `KNOWN_BILLING_SCHEME_IDS` is reported as a `NOTE` rather than rejected,
since that list is incomplete and an unknown value may be a new scheme or a
typo. Add schemes to that set as you confirm them.

**What `validate()` cannot do:** it checks that values are present and shaped
like UUIDs — not that they exist in Famly, belong to this child, or mean what
the producer intended. A well-formed UUID for the wrong session passes
validation and still produces a wrong plan. Only a preview catches that, and
only through its warnings.

#### From CSV

One row per booking line; rows sharing a `childId` become one plan with one
plan part. A row carries **either** a `sessionId` or a `productId`. Plan-level
values are read from that child's first row, so later rows need only their
booking columns. See `samples/plans.sample.csv` for the column order.

```bash
python main.py plan --mode preview-csv --file plans.csv --version 3
```

Each plan is validated first. Invalid ones print their errors and are **never
sent to Famly**; valid ones are previewed and their warnings printed. This mode
cannot commit.

Real CSVs and plan payloads are gitignored (`*.csv`, `*.plan.json`) because they
carry child data — only the sample is committed.

#### From HubSpot

`ruleGroupId`, `planParts[].billingProfileId`, `planParts[].attendanceScheduleId`,
`planParts[].billing.*` and (for a non-funded deal's add-ons) `product_1_id`/
`product_2_id` are no longer read from the HubSpot payload at all --
`actions/plan_write/hubspot_flatten.py` resolves them from
`reference/institution_defaults.json` (see "Reference-data pulls" above) using
the deal's `institution` and `termTimeOnly`. HubSpot sending any of them is a
no-op.

**End dates.** In a flat HubSpot payload, `to` is the **last day the child is
covered** (the leave date, inclusive). Famly's plan `to` is the first day *not*
covered, so the plan body sends `to` plus one day (`2026-12-31` becomes
`2027-01-01`, `2028-02-29` becomes `2028-03-01`; empty stays null). The
conversion happens once, in `hubspot_flatten.exclusive_end`, and nothing after it
touches `to`. `from` is unchanged. Slack headings and the `create_timeline`
segments keep the inclusive date; the Slack summary's "Dates" line shows the last
covered day too. A plan read back from Famly carries the exclusive date.

`actions/plan_write/hubspot_intake.py` accepts the same payload, nested under
`properties` or `data` or neither:

```python
from actions.plan_write.hubspot_intake import handle_intake

result = handle_intake(payload, version=3)   # dry_run=True by default
if not result.ok:
    ...  # result.errors -- nothing was sent to Famly
```

Validation runs **before** any network call, so a malformed payload costs
nothing. `dry_run=False` raises `NotImplementedError`: committing from an
automated intake path is deliberately not wired up, because a commit must go
through `runner.commit` with an explicit confirm and a test-child allow-list
that a webhook cannot satisfy on its own.

There is no HTTP server here and none belongs in this package. A Flask/FastAPI
shell can call `handle_intake` once the read-only path is proven; it belongs in
`integrations/`, subject to the rule in that folder's README.

## The intake server

`server.py` is a thin Flask **router**. It authenticates, reads an `action`
field from the JSON body, dispatches to that action's handler via
`web_registry.py`, and wraps the result in one standard envelope. It holds no
per-action logic and imports no `actions.*` module — the registry does that.

### ⚠️ HubSpot webhook change required

The webhook previously sent plan fields at the top level with no `action`
field. **It must now send `"action": "plan_preview"` in the body.** Without it
the request is rejected with 400 and a message listing the valid actions —
update the HubSpot webhook before relying on this.

```json
{
  "action": "plan_preview",
  "childId": "...",
  "from": "2026-09-01",
  "monday_session": "..."
}
```

Everything else about the payload is unchanged: plan fields at the top level,
or nested under `properties`/`data`.

### Routes

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | none | liveness check; no secrets, no Famly calls |
| `POST /intake` | `X-Intake-Secret` header | dispatches by `action` |
| `POST /slack/interactivity` | Slack signature | records button clicks (inert) |

### Response envelope

Every action returns through the same shape:

```json
{"ok": true, "action": "plan_preview", "data": {}, "warnings": [], "errors": []}
```

For `plan_preview`, `data` holds `plan` (the flattened summary), `previewed`,
`preview_id` and `slack_posted`. The full computed plan is logged rather than
returned -- see [Slack approval flow](#slack-approval-flow). `warnings` and
`errors` come from the intake result. `ok` is true only on a successful
preview.

### Status codes

| Situation | Status | Retry? |
| --- | --- | --- |
| Successful preview | 200 | — |
| Missing or unknown `action` | 400 | no |
| Missing/invalid shared secret | 401 | no |
| Validation rejected the payload | 422 | **no** |
| Famly returned 4xx | 422 | **no** |
| Famly returned 5xx | 502 | yes |
| Anything else | 500 | yes |

The 4xx→422 mapping is deliberate: a Famly 4xx means the payload is wrong and
will never succeed, so HubSpot must not retry it. Only a Famly 5xx is a real
transient fault. This classification lives in the router, so every action
inherits it.

### Slack approval flow

After a successful preview, the `plan_preview` action posts a readable summary
to Slack with **Approve** and **Reject** buttons, and returns a `preview_id`
and a `slack_posted` flag alongside the plan summary.

```
HubSpot -> POST /intake -> preview -> Slack message (Approve / Reject)
                                          |
                        POST /slack/interactivity  (signature-verified)
                                          |
                                    logged only - nothing commits
```

**Approve commits the plan** -- see
[Committing an approved plan](#committing-an-approved-plan). Reject records the
decision and writes nothing.

The message is updated by POSTing to the interaction's `response_url`, not by
the inline response: the preview was posted proactively with `chat.postMessage`,
and an inline `replace_original` does not reliably update a message Slack did
not render itself. The endpoint acks with an empty 200. If an interaction ever
arrives without a `response_url`, it falls back to the inline response.
The commit path is a deliberate later step, marked with a `TODO` in
`server.py`. `handle_intake` remains dry-run only and raises if asked to write.

#### What is returned, and what is not

`plan_full` is **no longer returned** to HubSpot. It is logged server-side at
INFO, keyed by `preview_id`:

```
PLAN PREVIEW preview_id=<uuid> plan_full={...}
```

That log line is currently the only record of a previewed plan -- there is no
store yet, so a future commit path will need one (or will have to re-preview
from the original payload). The flat `plan` summary is still returned.

A Slack outage cannot break a preview: `post_preview` catches everything,
returns False, and the response is still 200 with `slack_posted: false`.

#### Inbound security

`POST /slack/interactivity` does **not** use `X-Intake-Secret` -- Slack
authenticates by signing the request. `verify_slack_request` checks the
HMAC-SHA256 signature over the **raw** body (re-serialised form data would not
match) and rejects anything older than five minutes, for replay protection. It
fails closed: with no `SLACK_SIGNING_SECRET` set, every request is rejected.

Verification is fully implemented now, before any write is attached to it, so
the security is proven first. Forged signatures, tampered bodies, replays and
missing headers all return 401.

#### Slack app setup

| Setting | Value |
| --- | --- |
| Interactivity request URL | `https://<domain>/slack/interactivity` |
| Bot scope | `chat:write` |
| Env | `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`, `SLACK_SIGNING_SECRET` |

### Committing an approved plan

Clicking **Approve** in Slack writes the plan to Famly. It is the only write
path in the project, and it commits the EXACT body that was previewed, read
back from a store -- never a rebuild, so what is written is what the approver
saw.

Five guards, in order. Any one of them stops the write:

| Guard | Where | Refusal |
| --- | --- | --- |
| `COMMIT_ENABLED` must be true | `core/config.py` | "Commit is disabled" |
| Preview must exist and be within its TTL (24h) | `preview_store.get` | "expired or not found" |
| Not already committed / rejected / in progress | atomic claim | "already committed" |
| Child must be in `COMMIT_ALLOWED_CHILD_IDS` | config + `runner.commit` | "child not in the allowed list" |
| `confirm=True` and a non-empty allow-list | `runner.commit` | refusal surfaced to Slack |

**The first real commit must target a disposable test child.** Set
`COMMIT_ALLOWED_CHILD_IDS` to that child's ID and nothing else, then turn
`COMMIT_ENABLED=true`. Widen the list only once the path is proven.

```bash
COMMIT_ENABLED=true
COMMIT_ALLOWED_CHILD_IDS=<test-child-uuid>
```

Scope: **create only**. Overwriting an existing plan and adding a second plan
are separate operations and are deliberately not built.

#### Double-click safety

Two Approve clicks would otherwise both read "pending" and write two real
plans. The store moves `pending -> committing` in a single conditional UPDATE,
so exactly one click can win; the second is told it was already committed. A
failed commit releases the claim, so a corrected retry is possible.

#### The preview store

`actions/plan_write/preview_store.py`, SQLite via stdlib `sqlite3` (no
dependency, and safe across gunicorn workers). It holds child plan data, so it
lives in `var/` and is gitignored. `PREVIEW_STORE_PATH` moves it;
`PREVIEW_TTL_HOURS` changes the 24h expiry.

An approval clicked days later is refused rather than committed against stale
pricing.

### Adding a web action

Create `actions/<name>/web.py` exposing `ACTION_NAME` and
`handle(payload) -> (data, status)`, then add one entry to
`web_registry.ACTIONS`. `server.py` does not change. Handlers are plain
functions — unit-testable without Flask or a running server.

## The unattended queue (`queue_timeline` + `queue-worker`)

`create_timeline` builds a starter's plans one Slack approval at a time. The queue
builds the same timelines **unattended**: HubSpot (or anything else) posts the
request, it is validated and stored at once, and a separate worker process builds
it later, committing the plans that are clean and holding the ones that are not.

### Accepting a request

`POST /intake` with `"action": "queue_timeline"` and the same payload as
`create_timeline` (plus `auto_round`). It validates **every** plan with no Famly
call, stores the allow-listed payload (no names) in the `jobs` table and returns
the job id straight away. The same child, start date and payload returns the
existing job instead of making another (a failed or cancelled job does not block
asking again). It reads nothing from Famly or Slack.

### The worker

```bash
python main.py queue-worker          # runs until stopped
python main.py queue-worker --once   # recover, run at most one job, exit
```

It runs **one job at a time**. For each job it reads the child's plans (the child
must have none), then for each plan: preview, auto-round, clean-check, commit,
and the read-back. Commits go through the **existing approval path**
(`approval._approve`), so `COMMIT_ENABLED`, `COMMIT_ALLOWED_CHILD_IDS`, the atomic
claim and the live check of the child's plans all apply exactly as they do to a
human clicking Approve.

`QUEUE_MODE`:

| mode | behaviour |
|---|---|
| `off` | the worker does nothing, and `queue_timeline` refuses requests |
| `dry_run` (**default**) | commits **nothing**. Each plan is previewed on its own (a later plan cannot be chained to one that does not exist yet), rounded and clean-checked, and the result goes in a Slack digest |
| `auto` | commits clean plans, in order |

A plan is **clean** only if it validated, raised no warning (the flattener's or
Famly's, apart from the informational funding-limit note), rounding succeeded or
was not requested, and every period's estimate is above 0 and below
`QUEUE_MAX_MONTHLY`. A plan that is not clean is **not committed**: the job is
**held** and the normal Slack approval message is posted for that plan, with the
reasons. When someone approves it the job goes back on the queue and the worker
carries on; rejecting it cancels the job. A held plan that expires unanswered
cancels the job, with a notice.

### Safety rails

| variable | meaning |
|---|---|
| `QUEUE_ALLOWED_INSTITUTIONS` | comma-separated codes allowed to commit automatically. **Empty means none**: every plan is held for approval |
| `QUEUE_MAX_COMMITS_PER_DAY` | commit attempts per UTC day (default 20), checked before **every** commit. The rest of the batch waits for the next UTC day, with a Slack notice |
| `QUEUE_MAX_MONTHLY` | a plan whose estimate is at or above this is held (default 5000) |

* **Circuit breaker:** after 3 jobs in a row fail, the queue pauses itself and posts to Slack.
* **Manual pause:** `queue-pause` / `queue-resume`. The flag is checked before every
  commit, not only at the start of a job, so a pause takes effect mid-job.
* **Failures:** a commit is **never retried**. After any commit that did not report
  success the worker reads the child's plans; a plan that is there was written and
  counts as a success. Reads and previews retry up to 3 times with backoff (30s
  steps on HTTP 429). On startup a `running` job is resumed by **read-back**,
  never by replaying.

### Slack

Held and failed jobs post **immediately**, naming the child. Successes go in one
digest when the queue drains, or every 30 minutes while it is busy.

### Commands

```bash
python main.py queue-status                 # mode, pause flag, counters, job counts
python main.py queue-list                   # every job
python main.py queue-list --status held     # one status
python main.py queue-cancel <job>           # never deletes anything in Famly
python main.py queue-pause
python main.py queue-resume                 # also clears the failure count
```

### Running it under systemd

The worker is its **own** unit, separate from gunicorn, so restarting the web
server never interrupts a job (and a worker restart is safe: it resumes by
read-back).

```ini
# /etc/systemd/system/famly-queue-worker.service
[Unit]
Description=Famly queue worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=drew
WorkingDirectory=/home/drew/famly
EnvironmentFile=/home/drew/famly/.env
ExecStart=/home/drew/famly/venv/bin/python main.py queue-worker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now famly-queue-worker
journalctl -u famly-queue-worker -f
```

Put `QUEUE_MODE`, `QUEUE_ALLOWED_INSTITUTIONS` and the other variables above in the
same `.env` the web server reads (the worker shares the preview store file, so
`PREVIEW_STORE_PATH` must match). Leave `QUEUE_MODE` unset (dry run) until the
digests look right, then set `auto`.

## `delete_plans` (TEMPORARY, DESTRUCTIVE)

A one-off clean-up action that deletes **all** of one child's plans per webhook.
Delete the whole `actions/delete_plans/` package, its entry in `web_registry.py`
and its tests when the clean-up is finished. `live` mode **really deletes**: for
each plan it sends one `DELETE v2/plans/<id>` (no params, no body; Famly answers
HTTP 200 with `[]`, which is not treated as proof). Only `dry_run` is harmless.

```json
{"action": "delete_plans", "childId": "<uuid>", "confirm": "DELETE"}
```

`confirm` must be exactly `DELETE`; `childId` must be a UUID. The guards:

| setting | meaning |
|---|---|
| `DELETE_PLANS_MODE` | `off` (the default; an unknown value counts as off), `dry_run` or `live` |
| `DELETE_PLANS_ALLOWLIST_FILE` | one child id per line, default `var/delete_plans_allowlist.txt` (gitignored). Re-read on every request. A missing or empty file allows nothing |
| `DELETE_PLANS_MAX_PER_DAY` | the most **children** touched per UTC day (default 40), checked before every delete |
| `DELETE_DIGEST_ON_COMPLETE` | default `false`: digests come only from `delete-digest`. `true`: a webhook request also posts the digest once every allowlist child has reported (bare ids) |
| `DELETE_PLANS_BACKUP_DIR` | where backups go, default `var/plan-backups` (gitignored: personal data) |

In `live` mode each child's plans are deleted latest-starting first. A plan without
`CanDeletePlan` is skipped and reported. Each plan's full read-back JSON is written
to `var/plan-backups/<child>/<plan>.json` **first** (no backup, no delete); the plans
are read back afterwards, and that read-back -- never the `[]` -- decides. A delete is
**never retried**. "Unexpected" means exactly one of: the plan is **still present** in
the read-back, an **HTTP error**, or the child's **plan count not dropping by exactly
one**; it stops that child and posts to Slack at once. A change to a neighbouring plan's
end date is logged and listed in the digest but **never stops the run**, since every
plan is being deleted anyway. Every decision is a row in the `delete_log` table.
After 3 consecutive failed children a **pause flag** is set (and Slack told); it is
checked before every delete. The flag is its own key in `queue_state`
(`delete_plans:paused`), separate from the plan queue's `paused`, so tripping one
never pauses the other.

```bash
python main.py delete-resume   # clears the delete_plans pause flag and shows the state
```

### Slack: one digest, and three immediate messages

`delete_plans` does **not** post a message per child. Only three things post at
once: an unexpected result that stops a child, a tripped pause (3 failed children
in a row) and a daily-cap stop. Everything else -- deleted, nothing to delete,
partly deleted, refused (including a child stopped by an already-set pause), a
dry run -- goes in **one end-of-run digest**.

* **When it posts.** By default a digest comes **only from the `delete-digest`
  command**: a webhook request never posts one. With `DELETE_DIGEST_ON_COMPLETE=true`
  (default `false`; read on every request; anything but `true`, `1`, `yes` or `on`
  counts as false) a webhook request also posts it after each child, once every
  child on the allowlist has an unreported `delete_log` row -- with bare ids, never
  names. The three immediate messages (an unexpected stop, a tripped pause, a
  daily-cap stop) are the same either way. `python main.py delete-digest`
  posts whatever is unreported now, listing any allowlist child with no row as
  **Not received**. `python main.py delete-digest --if-quiet 10` posts only if
  there are unreported rows and the newest is at least 10 minutes old, so it can
  run from cron every 5 minutes.
* **At most once.** `delete_log.reported_at` (added in place) is stamped when a row
  goes into a digest; a row is posted in one digest only, and a row that arrives
  after a digest starts the next one.
* **What it says.** A header with the mode and the counts (deleted, nothing to
  delete, partly deleted or skipped, failed, refused, not received); a section per
  category with one line per child -- name, the id in code formatting, the number of
  plans deleted, the reason for a failure, and in a dry run the plans (from, to,
  monthly estimate) that would be deleted; and the backup folder at the end. Over
  Slack's size limit it is split into numbered messages (1/2, 2/2).
* **Names.** Fetched at digest time with the existing 3-second lookup, falling back
  to the bare id, and never stored. They are never fetched during a webhook
  request, so the digest a webhook triggers (only with `DELETE_DIGEST_ON_COMPLETE=true`)
  shows bare ids only; the `delete-digest` command shows names.
* A neighbouring plan's end date that moved after a delete is in the `delete_log` row
  for that plan (`detail`) and is listed on that child's digest line ("neighbour end
  dates changed: ..."); it never stops the run.

`delete-resume` prints the mode, whether it is paused, the consecutive-failure count,
how many children were done today, and the last five `delete_log` rows (newest
first). It clears only the delete_plans flag; `queue-resume` is the plan queue's.

### Responses

The HTTP status is **200 for every outcome, failures included**, so HubSpot never
retries a destructive call; only a malformed request is 422. The envelope's own
`ok` mirrors HTTP 200, so read **`data.ok`** and **`data.outcome`**:

| `data.outcome` | `data.ok` | meaning |
|---|---|---|
| `malformed` | false | HTTP **422**: a bad `childId` or a `confirm` that is not exactly `DELETE`. Nothing was done |
| `off` | false | `DELETE_PLANS_MODE` is off, unset or unknown. Nothing was done, logged or posted |
| `not_allowlisted` | false | the child is not on the allowlist file. Nothing was read or done; it is logged and reported in the digest |
| `nothing_to_delete` | true | the child has no plans. A harmless no-op, so a repeat request lands here |
| `dry_run` | true | `dry_run` mode: the plans were logged (and appear in the digest). Nothing was deleted |
| `deleted` | true | `live`: every plan was deleted, each backed up first and confirmed gone by read-back |
| `partial` | false | `live`: every plan that could be deleted was, but at least one was skipped for lacking `CanDeletePlan` and remains |
| `failed` | false | `live`: the child was stopped by an unexpected result (a backup, delete or read-back failure, the plan still present, the plan count not dropping by exactly one, the pause flag, or the daily cap). See `data.message` |
| `error` | false | an unexpected error, for example Famly could not be read. Reported, never raised |

The `delete_log` table records finer-grained outcomes per plan and child:
`started` (a live child began, counted against the daily cap), `deleted`,
`skipped`, `backup_failed`, `delete_failed`, `readback_failed`, `unexpected`,
`paused`, `cap_reached`, plus the response outcomes `dry_run`, `nothing_to_delete`,
`not_allowlisted` and `error`. Each live child request also ends with ONE terminal
row (no plan) that the digest reads: `done` (all deleted), `partial` (some skipped),
`stopped` (an unexpected result) or `refused` (the pause flag or the daily cap), its
`detail` JSON carrying the number deleted and skipped and the message. A dry run's
`dry_run` row carries the plans that would be deleted. `reported_at` is NULL until
the row goes into a digest.

## Reference-data pulls (sessions, groups, products, institution defaults)

Four maintenance commands refresh the local JSON reference files that other
actions read, replacing manual copy-paste from Famly:

| Command | Writes | Status |
| --- | --- | --- |
| `python main.py pull-sessions` | `reference/session_catalogue.json` **and** `reference/session_titles.json` | live -- the catalogue is functionally load-bearing (booking logic reads it); the titles file is display-only (see "Readable names in Slack") |
| `python main.py pull-groups` | `reference/groups_catalogue.json` | live -- informational only today, nothing reads it yet |
| `python main.py pull-products` | `reference/products_catalogue.json` **and** `reference/institution_defaults.json`'s `addonProducts` | live -- the flat map is informational; `addonProducts` is functionally load-bearing for non-funded add-on bookings (see below) |
| `python main.py pull-institution-defaults` | `reference/institution_defaults.json` | live -- functionally load-bearing (`hubspot_flatten` reads it for every plan part; see below) |

Each is independently runnable, and all four share the same conventions:

- `--institution CODE` (repeatable) restricts the pull to one or more sites
  from `reference/catalogue.json`'s `sites` section; an unrecognised code is a
  clear error before anything is fetched, never a silent no-op.
- `--dry-run` fetches and reports what would change without writing a file or
  a backup.
- The existing file is always backed up first, timestamped alongside it
  (`<name>.<UTC timestamp>.bak.json`), before anything is overwritten.
- Writing **merges** into the existing file -- an institution (or a group/
  session/product within one) this run did not touch is left exactly as it
  was, never wiped by a partial or filtered run.
- Anything Famly returns that cannot be cleanly matched to a configured
  institution is reported (not guessed at) and written nowhere.

`python main.py pull-references [--institution CODE] [--dry-run]` runs all
four together under one command, for a single weekly cron entry. One pull
failing entirely (a bad `--institution` filter or a connection error) is
reported for that pull only and does not stop the others from running and
writing.

**`institution_defaults.json` is written by TWO pulls, each owning a different
key:** `pull-institution-defaults` owns `ruleGroupId`/`schedules`, `pull-products`
owns `addonProducts`. Each merges onto an institution's existing entry rather
than replacing it wholesale, so running one never wipes out what the other
already wrote.

### `pull-institution-defaults`: pricing rule group + billing-profile defaults

HubSpot used to send `ruleGroupId`, `attendanceScheduleId`, `weeksOfCare`,
`billingId`, `billingTitle` and `billingInvoices` as flat, institution-wide
constants on every webhook -- values that never actually varied per deal, only
per institution (and, for the billing-profile fields, per term-time-only vs
full-year schedule). `billingProfileId` was never sent at all and reached
Famly as a bare `null`.

**HubSpot no longer sends any of those seven fields.** `pull-institution-defaults`
pulls them from Famly instead (two internal-API queries per institution --
`GetInstitutionSingleRuleGroupQuery` and `GetBillingProfiles`, see
`actions/pull_institution_defaults/runner.py`'s module docstring for the exact
shapes), and `actions/plan_write/hubspot_flatten.py` resolves them via
`integrations/institution_defaults.py`, keyed by the deal's `institution` and
a schedule bucket (`all_year_round` / `term_only`) derived from its
`termTimeOnly` flag. If HubSpot still sends any of the seven fields, they are
silently ignored -- a no-op, not an override.

This is **money-relevant, so the pull never guesses**: an institution's billing
profiles must bucket into exactly one `all_year_round` and exactly one
`term_only` profile (after filtering out deleted ones), and the two buckets'
billing schemes must agree with each other -- zero/multiple profiles in a
bucket, a profile with no attendance schedule to bucket it by, or mismatched
billing schemes across buckets are all hard, reportable anomalies for that
institution. An institution either gets a **complete** fresh entry (rule group
+ both schedule buckets) or none at all; there is no partial write.

### `pull-products`' `addonProducts`: the two fixed non-funded add-on products

HubSpot used to send `product_1_id` and `product_2_id` on a non-funded deal
alongside `addon_quantity`. **It no longer sends either.** `addon_quantity`
alone now signals "book the two fixed add-on products", and
`actions/plan_write/hubspot_flatten.py` resolves both product ids from
`reference/institution_defaults.json`'s `addonProducts` (via
`integrations/institution_defaults.py`'s `resolve_addon_products`), keyed by
the deal's `institution`. Sending `product_1_id`/`product_2_id` anyway is a
no-op, same as the seven fields above.

`pull-products` resolves `addonProducts` for each institution by an **exact,
case-sensitive** title match against Famly's product list -- `"Meals & Snacks"`
-> `mealsProductId`, `"Educational Activities & Extras"` -> `activitiesProductId`.
Deliberately no normalization: these are fixed, known titles, and normalizing
could mask a real naming drift in Famly (which is exactly what happened during
this feature's build-out -- titles were briefly inconsistent across
institutions while being corrected by hand). Zero or 2+ products matching
either title is a hard, reportable anomaly for that institution and blocks
only the `addonProducts` write -- the flat `products_catalogue.json` entry for
that institution is written regardless, since it is a separate, unconditional
concern.

**Half days.** `addon_quantity` is days, in halves. `full = min(floor(q), 3)`
whole days book both full products on the first `full` booked days; if `q` ends
in `.5` and `floor(q) < 3`, one more booked day gets both half products,
`"(1/2) Meals & Snacks"` -> `halfMealsProductId` and `"(1/2) Educational
Activities & Extras"` -> `halfActivitiesProductId` (same exact-title matching).
So 2.5 is two full days plus one half day; 3.5 and above book three full days
(the half is never added on top of the cap); any other fraction (2.3) floors.
`full + half` exceeding the booked days is a hard error. A missing or
duplicated half title is reported by the pull (`halfProductsFailed`) but never
blocks the full pair; it only fails a booking that actually needs a half, with
an error naming the missing title. The old `half_day_adjustment` /
`half_day_amount` discount is gone and those fields are ignored.

### Readable names in Slack

The Slack preview names sessions and products automatically, from the pulled
catalogues, without anyone maintaining a name list. `pull-sessions` records the
RAW Famly title of every session it sees in `reference/session_titles.json`
(including ones it cannot classify for booking); `pull-products` already stores
raw product titles in `products_catalogue.json`. The readable wording is derived
from those at read time by `integrations/display_names.py` and **never stored**,
so changing the wording needs no re-pull:

| Famly title | Slack shows |
| --- | --- |
| `(F) Morning` | Funded Morning |
| `(FNM) Full Day` | Funded Full Day (No Meals) |
| `(FNA) Afternoon` | Funded Afternoon (No Activities) |
| `(FNE) Morning` | Funded Morning (No Extras) |
| `Afternoon` | Afternoon |
| `(F) Meals & Snacks` / `(1/2) Meals & Snacks` / `(1/2,F) ...` | Funded ... / Half Day ... / Half Day Funded ... |

`catalogue.load_titles()` names each id by the first of: (a) the flat `sessions`/
`products` maps in `reference/catalogue.json`, kept as **manual overrides** (they
win, so a hand-written name there hides the derived one); (b) the derived name;
(c) the raw Famly title, which is what (b) returns for a title that does not
parse; (d) the raw UUID. A missing or malformed pulled file logs a warning and
falls through -- it can cost names, never a preview.

### Scheduling it weekly

Not installed by this repo -- add a cron entry pointing at the project's
Python interpreter, for example (Sundays at 03:00, adjust the paths):

```cron
0 3 * * 0 cd /path/to/famly-integration && /path/to/.venv/bin/python main.py pull-references >> var/pull-references.log 2>&1
```

Make sure the environment the cron job runs in has `FAMLY_ACCESS_TOKEN` (and
any of `FAMLY_CATALOGUE_FILE` / `SESSION_CATALOGUE_FILE` / `GROUPS_CATALOGUE_FILE`
/ `PRODUCTS_CATALOGUE_FILE` / `INSTITUTION_DEFAULTS_FILE`, if overridden)
available -- crontab does not load `.env` on its own, so either source it in
the command or configure the variables in the crontab/systemd unit itself.

### Refreshing on the droplet (`reference-refresh`)

`python main.py reference-refresh [--dry-run]` is the safe, scheduled version of `pull-references` for the
five generated files (`session_catalogue`, `session_titles`, `groups_catalogue`, `products_catalogue`,
`institution_defaults`). `catalogue.json` and `funding_rules.json` are hand-maintained and never touched.

**`REFERENCE_DIR`** moves the five files out of the repo (the droplet uses `/home/drew/famly-reference`, so a
pull never dirties tracked files and `git pull` keeps working). Per file the order is: the file's own
variable (`SESSION_CATALOGUE_FILE`, `SESSION_TITLES_FILE`, `GROUPS_CATALOGUE_FILE`,
`PRODUCTS_CATALOGUE_FILE`, `INSTITUTION_DEFAULTS_FILE`), then `REFERENCE_DIR/<file>.json`, then the repo's
`reference/`. Unset, nothing changes. Every loader re-reads its file on every call, so no restart is needed
after a refresh. The web unit, the workers and the cron job must all see the same `REFERENCE_DIR` (put it in
`.env`, which all of them load).

The command copies the live files to `REFERENCE_DIR/.staging`, runs the pulls against the copies, and
replaces nothing if any pull reports an error for any institution. Each staged file must be valid JSON,
not empty, and must not have lost more than 20 percent of its leaf entries (exactly 20 percent is allowed);
otherwise every live file is kept and Slack gets file, old and new counts (exit 1). When all pass, each
changed file is backed up (`<name>.<UTC stamp>.bak.json` beside it), replaced with `os.replace` (a failure
part-way restores the files already replaced), and the backups are pruned to the newest 5 per file. Slack
gets one summary (ids and counts only), with its own `:warning: pricing inputs changed` line when
`institution_defaults.json` changed; nothing is posted when nothing changed. `--dry-run` prints the
validation, the diff counts, the number of Famly calls and the duration, and replaces and posts nothing.
Exit 0 on success or no change, 1 on any refusal or error (including a failed Slack post after a replace).

**Pricing-input guards.** (1) A pull that cannot resolve an institution's half-day add-on pair
(`half_products_failed`) now KEEPS the half ids the file already has, and a refresh whose staged
`institution_defaults.json` lacks a half pair the live file has is refused. (2) When two Famly sessions parse
to one slot, `pull-sessions` writes nothing for the slot (the existing id stays) and reports every session
claiming it as unmatched with reason `ambiguous_slot`. (3) Unmatched sessions: the ids already reviewed are kept
in `REFERENCE_DIR/unmatched_sessions_known.json`. None unmatched: pass. Some unmatched and no recorded set:
refused until you review them and run `reference-refresh --record-unmatched-baseline` once. After that a refresh
is refused only for an id not in the set (Slack lists the new ids and counts); review it and add it with
`reference-refresh --accept-unmatched SESSION_ID` (repeatable; the id must be unmatched now; logged). Ids that
stop being unmatched leave the set. The set is written only after a successful run, never on a refusal.
Product and group unmatched stay warnings. `--dry-run` shows per institution the unmatched sessions, ambiguous
slots and half-pair status (`present`, `absent`, `failed_kept`, `failed_missing`).

`onboard-quality-check` logs the resolved reference directory and each file's age on every run and adds one
Slack line when any of the five is older than 3 days (or missing).

Cron (droplet user `drew`; not installed by the repo; `mkdir -p logs` once):

```cron
CRON_TZ=Europe/London
10 3 * * * cd /home/drew/famly && flock -n /tmp/reference_refresh.lock /home/drew/famly/venv/bin/python main.py reference-refresh >> logs/reference.log 2>&1
```

If the droplet's cron ignores `CRON_TZ` (see the quality-check note in `docs/status.md`), 03:10 London is
03:10 UTC in winter and 02:10 UTC in summer.

### Plan status on the deal (`famly_plan_status`, `famly_plan_note`)

The server writes two HubSpot deal properties that workflows can wait on (they replace `famly_plan_uploaded`):
`famly_plan_status` (dropdown: `plan_created`, `needs_attention`) and `famly_plan_note` (a short CODE, never
free text, a name or an email). Each is written with one PATCH, never retried and never blocking Famly; a
missing property or a dropdown value HubSpot does not offer is logged, posted to Slack as a code, and skipped.
The note is written first, then the status.

- `plan_created` (note cleared): after a commit is confirmed by a live read of the child's plans, for every
  plan including one added alongside another. A later success overwrites an earlier `needs_attention`.
- `needs_attention` with a note code: `plan_rejected:<input_invalid|session_catalogue|institution_defaults|
  addon_products|famly_http_<status>|timeline_invalid|segments_invalid>` (preview or request rejected),
  `commit_failed_http_4xx`, `commit_failed_http_5xx`, `commit_failed` (a direct commit failed),
  `commit_unknown` (committed but not confirmed after the live reads), `queue_job_failed[:kind]`,
  `chain_failed[:kind]`. The deal id comes from the webhook body (`dealId`), the stored preview, the job or the chain.
- A rejection note also says WHICH rule failed, as up to three comma-joined sub-codes and then `,+N`, for example
  `plan_rejected:input_invalid:start_date_missing,no_bookings,+2` or `plan_rejected:segments_invalid:day_slot_invalid`
  (the fixed table is `SUBCODE_RULES` in `actions/plan_write/status_codes.py`: one code per validation rule, e.g.
  `child_id_missing`, `start_date_missing`, `start_date_invalid`, `end_date_invalid`, `leave_before_start`,
  `no_bookings`, `day_slot_invalid`, `institution_missing`, `defaults_incomplete`, `addon_exceeds_days`,
  `funding_invalid`; anything unrecognised is `other`). Never raw text or values. Every rejected preview, and
  every rejected queue or chain request, also posts ONE Slack line: the deal id and the code only.
- `python main.py plan-check --file ids.txt` (read-only, max 60 deals): reads each deal from HubSpot (GET only)
  and prints, per deal and counted by sub-code, which of those rules its plan inputs would fail. It is PARTIAL:
  it rebuilds only the inputs the repo reads from the deal (child id, start and end date, site, weekday slots,
  `billable_fees`) and runs the same intake functions on them. The rest of the plan_preview body (funded,
  meals and activities, termTimeOnly, add-ons, discounts, site_code, note) is built by the workflow's custom-code
  action, which is not in this repo, so a deal that passes can still be rejected for those. It also reports
  `fees_missing`, `fees_unparseable` and `site_unknown`, which only it can see.
- `PLAN_FLAG_LEGACY_WRITE` (default on; only `off` disables it) also sets `famly_plan_uploaded = true` after a
  confirmed first plan. Delete the old property, then set it to `off`. `hubspot-set-plan-uploaded` keeps working.

Backfill: `python main.py hubspot-set-plan-status --from-famly (--operations | --file PATH) [--confirm]`. A dry
run unless `--confirm`. It writes `plan_created` only where a live read shows the child has a plan AND the
deal's status is empty; it never overwrites a status, never writes `needs_attention`, never writes or clears
a note, skips deals without `famly_childid` or with an unreadable plan list, and writes at most 60 per run
(`--operations` defers the rest; run again). `--operations` covers the Operations deals in the shared scope
(Leave Date on or after `ONBOARD_SCOPE_LEAVE_AFTER`, default 2026-01-01, or no Leave Date and not Left Nursery;
`--leave-after YYYY-MM-DD` and `--all-dates` override it; the output reports counts by reason).

## The daily people-roster cache (`pull-roster`)

`python main.py pull-roster [--institution CODE] [--dry-run]` refreshes a
**separate, local, gitignored** SQLite cache of Famly children/contacts/
bill-payer identifying fields, for the upcoming contact/child-creation
pipeline to match against. It shares `pull-sessions`/`pull-groups`' backup,
per-institution-isolation and `--institution`/`--dry-run` conventions, but is
otherwise independent: it is **not** part of `pull-references`, runs on its
own daily schedule, writes SQLite instead of JSON, and -- notably -- talks to
a **different Famly endpoint**.

### Different endpoint

Every other GraphQL action here (`staff_credentials`, `pull_groups`,
`urn_lookup`) uses Famly's **internal** app API at `FAMLY_GRAPHQL_URL`
(default `https://app.famly.co/graphql`). That schema has no bulk way to list
a child's `externalId` or list contacts/bill payers the way this pull needs.
`pull_roster` instead uses Famly's **separate public API**
(`FAMLY_PUBLIC_GRAPHQL_URL`, default `https://famlyapi.famly.co/v1/graphql`),
confirmed live: `children { listBySiteIds }` (contacts come back nested per
child, so no second call is needed) and `billPayers { listBySiteIds }`, both
paginated via their own real cursor arguments. Same `FAMLY_ACCESS_TOKEN`,
different URL -- see `actions/pull_roster/runner.py`'s module docstring.

### ⚠️ This cache holds real child/family data -- never commit or back it up

`roster_cache.db` (default `var/roster_cache.db`, override with
`ROSTER_CACHE_PATH`) is treated **exactly like `.env`**: it is explicitly
gitignored (see `.gitignore`), and it **must never be included in any backup
or export that leaves the server**. It exists only for the local matching
functions below.

### What this cache is for, and what it must NEVER be used for

`actions/pull_roster/store.py` exposes normalized lookups (trim + casefold,
the same convention `actions.urn_lookup` uses):

```python
from actions.pull_roster import store

store.find_child_by_external_id(urn)
store.find_contact_by_email(email)
store.find_bill_payer_by_email(email)
```

These are for **matching an already-known identifier** so the
contact/child-creation pipeline can link the record it is about to write --
never for deciding whether a child, contact or bill payer **already exists**
before creating one. That existence check must be a **live Famly call made at
creation time** (per the idempotency design already agreed) -- this cache can
be up to 24 hours stale, so using it for a pre-creation check risks creating a
duplicate Famly already has, or skipping a creation for something already
gone. When that pipeline is built, its existence check must carry a comment
pointing back to this paragraph, and must not import `store`'s lookup
functions for that purpose. See `store.py`'s module docstring for the full
reasoning, and `tests/test_pull_roster.py`'s `ExistenceCheckGuardTests` for
the TODO that enforces it once the pipeline exists.

### Scheduling it daily

Also not installed by this repo -- a separate, daily cron entry (unlike
`pull-references`' weekly one), for example (every night at 02:00):

```cron
0 2 * * * cd /path/to/famly-integration && /path/to/.venv/bin/python main.py pull-roster >> var/pull-roster.log 2>&1
```

Same note on environment variables as `pull-references` above --
`FAMLY_ACCESS_TOKEN` (and `FAMLY_PUBLIC_GRAPHQL_URL`/`ROSTER_CACHE_PATH`, if
overridden) must be available to the cron job's environment.

## `contact_lookup` (HubSpot contact -> Famly ids)

Read-only web action. A HubSpot workflow POSTs a contact's email and writes the
returned ids onto the contact as properties:

```
POST /intake   (X-Intake-Secret)
{"action": "contact_lookup", "email": "..."}
```

`data` holds only `contactId`, `contactMatched`, `billPayerId`,
`billPayerMatched` and `source` (`cache`, `live` or `cache+live`); warnings and
errors ride in the envelope. No names or other emails are returned, and the
email is never logged.

- The roster cache is checked first. If either id has no cache match, Famly is
  fetched live once (every site in `catalogue.json`), matched in memory, and
  held in a per-process cache for `CONTACT_LOOKUP_LIVE_TTL_SECONDS` (default
  300). Live rows are never written to `roster_cache.db`; `pull-roster` stays
  the only writer.
- 200: both lookups completed, including not-found (a contact with no bill
  payer, or the reverse, is normal and gets a warning).
- 409: either side has two or more distinct ids. Both ids are null so HubSpot
  writes nothing half-done; the counts and ids are in `errors`.
- 422: missing or malformed email. 503: the live fetch failed (the search was
  incomplete, so it is never reported as "not found").

Check from the server: `python main.py contact-lookup someone@example.com`.

This is a **lookup** for linking HubSpot records to existing Famly ids. It must
never be the existence check for the contact-creation pipeline (see the
warning above).

## Onboarding dry run (`onboard-dry-run`, phase 1)

`python main.py onboard-dry-run --deal-id <id>` (or `--deal-ids a,b,c` for a
batch with a counts-only summary at the end) prints, as JSON, what onboarding a
HubSpot deal into Famly would do. **It makes no writes to Famly or HubSpot.**
Design: `docs/famly_onboarding_pipeline_design.md`.

Needs `HUBSPOT_TOKEN` (private app Famly_Server_Key; read scopes only) and the
usual `FAMLY_ACCESS_TOKEN`. HubSpot is read through `core/hubspot.py`, which can
only send GET, always asks for an explicit property allow-list and never logs
values. Association labels (Bill Payer, Sales) are matched by label/internal
name and read from the v4 labels endpoints first; the run prints the labels and
type ids it found. Override with `HUBSPOT_BILL_PAYER_LABEL`,
`HUBSPOT_BILL_PAYER_TYPE_ID`, `HUBSPOT_SALES_LABEL` and friends.

Per deal it reports the stage and deposit state, site (from
`location_of_interest`: Westminster/City/Canada Water only), room (age in
completed months at the start date), the child lookup (deal-id reference, then
urn, both live), a status per contact (`present`, `would_create`, or a hold on an existing child)
and the quotes. Anything unsafe is a HOLD with a reason code (`site_unmapped`,
`group_missing`, `dob_missing`, `child_ambiguous`, `bill_payer_unclear`, ...).
Output holds ids, codes and counts only: no names, emails, phone numbers or
dates of birth. It never uses `roster_cache.db`.

On a child that already exists, a HubSpot parent whose email is not one of its
contacts is a hold (`contact_unmatched_on_existing_child`, with candidate ids only);
create is only planned for a child that does not exist yet or that the job itself
created. `googlemail.com` and `gmail.com`
compare equal; nothing else is fuzzy. Left Nursery deals use Voyagers and need a
start date and an end date (`end_date`, the Leave Date); the output says only
whether each date is present.

Per contact it also reports `phoneSource` (mobile, then work, then WhatsApp, else
none), `hasEmail`, `hasCompleteAddress` (street, zip and city), `hasFirstAndLastName`,
`emergencyContact` (the bill payer only) and a RELATION=Parent record, as booleans
and codes only, with the warnings `phone_missing` and `address_incomplete`.

## Onboarding phase 2: create the child and contacts (`onboard-*`)

Queued, Slack-approved creation of a child and the parent contacts in Famly for a
HubSpot deal. **Not deployed; no webhook triggers it yet.**

```
ONBOARD_MODE=off|dry|live        default off (enqueue refuses, worker does nothing)
ONBOARD_ALLOWED_DEAL_IDS=1,2     live writes ONLY for these (anything else: held, deal_not_allowlisted)
ONBOARD_TEST_DEAL_IDS=1          subset of the above: isTestChild true (never set otherwise)
HUBSPOT_PORTAL_ID=...            optional, for the deal link in Slack
```

```
python main.py onboard-enqueue --deal-id <id>     # queue a job (refuses when off)
python main.py onboard-worker --once              # process one job (the same shape as queue-worker; run without --once to poll)
python main.py onboard-status --deal-id <id>      # counts and codes only
```

`dry` runs the full live plan and posts the Slack message but writes nothing and
ends `dry_done`. `live` posts an Approve/Reject request (Gate A); on approve the
worker re-runs the plan and writes only if its hash is unchanged. It then creates
the child (one request, never retried; an unknown outcome is settled by a live
lookup, never a second create; `lastDay` is always sent: the deal's end date, or
the first 31 August on or after the 4th birthday), checks it is the one child the
deal-id lookup finds, compares the internal summary (differences are only flags),
creates each contact with **empty roles** (all of them tried independently, bill
payer first), reads each back, and writes `famly_childid`, a defaulted `end_date`
(only if still empty), `famly_sync_status`, `famly_sync_note` and
`famly_sync_gaps` to the deal. HubSpot never blocks Famly: a missing property is
warned about and its write skipped. Names, emails, phones, addresses and dates
of birth are held in memory only; the jobs table, Slack and the logs carry ids,
codes and hashes. See `actions/onboard/worker.py` and
`docs/famly_onboarding_pipeline_design.md`.

### HubSpot trigger (HTTP)

The webhook action sends, to the existing intake route:

```
POST https://<domain>/intake
X-Intake-Secret: <INTAKE_SHARED_SECRET>
Content-Type: application/json

{"action": "onboard_enqueue", "dealId": "<deal record id>"}
```

`dealId` may be a string or a number but must be digits only (else 400). The
request only enqueues (same function as `onboard-enqueue`) and returns the job id;
the worker does the rest. It answers 200 for `queued`, `already_queued`,
`already_done`, `refused_mode_off` and `refused_not_allowlisted`, so HubSpot's
retries only fire on a real fault (500). A job from the webhook is held
`stage_not_eligible` unless the deal is in the Operations pipeline at Deposit
Pending; a CLI job has no such rule.

## Tests

```bash
python -m unittest discover -s tests -v
```

Stdlib `unittest`, no extra dependency and no running server: `requests.post`
is swapped for a recorder, so the outbound JSON is asserted directly. Covers
`update_message`'s payload shape and failure isolation, the Slack signature
check (valid, tampered, replayed, unconfigured), and interaction parsing.

## Note on file URLs and expiry

Each assignment's `files[].url` is a **pre-signed S3 URL that expires roughly
two hours after the request**. Any file download must therefore happen in the
same run that fetched the URL — a URL stored now and fetched later will fail.
Do not persist these URLs and expect them to work. They are visible with
`--format full`.

This tool does not download files today; it is read-only and prints parsed data.

## Scope

Read-only by intent. The intake server previews plans and never commits: the
only registered web action is dry-run by construction. See
`integrations/README.md` before adding anything that writes to another system.
