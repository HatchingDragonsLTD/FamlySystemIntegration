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

`ruleGroupId`, `planParts[].billingProfileId`, `planParts[].attendanceScheduleId`
and `planParts[].billing.*` are no longer read from the HubSpot payload at all
-- `actions/plan_write/hubspot_flatten.py` resolves them from
`reference/institution_defaults.json` (see "Reference-data pulls" above) using
the deal's `institution` and `termTimeOnly`. HubSpot sending them is a no-op.

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

## Reference-data pulls (sessions, groups, products, institution defaults)

Four maintenance commands refresh the local JSON reference files that other
actions read, replacing manual copy-paste from Famly:

| Command | Writes | Status |
| --- | --- | --- |
| `python main.py pull-sessions` | `reference/session_catalogue.json` | live -- functionally load-bearing (booking logic reads it) |
| `python main.py pull-groups` | `reference/groups_catalogue.json` | live -- informational only today, nothing reads it yet |
| `python main.py pull-products` | `reference/products_catalogue.json` | **stubbed** -- Famly's public API has no products/billing-profile query; every institution reports "not yet implemented" until a real query is confirmed and wired into `actions/pull_products/` |
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
failing entirely (a bad `--institution` filter, a connection error, or
`pull-products`' stubbed state) is reported for that pull only and does not
stop the others from running and writing.

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
