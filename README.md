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
actions/<name>/query.graphql           the query text (GraphQL actions)
actions/<name>/runner.py               does the work, owns its response types
actions/<name>/cli.py                  argparse wiring only
actions/plan_write/builder.py          builds the plan write request body
actions/plan_write/warnings.py         classifies + escalates plan warnings
integrations/                          reserved, empty (see its README)
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

### Adding another action

Create `actions/<name>/query.graphql` and `actions/<name>/runner.py` with a
`run(...)` function, then add one entry to the `ACTIONS` dict in `main.py`.

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

## Note on file URLs and expiry

Each assignment's `files[].url` is a **pre-signed S3 URL that expires roughly
two hours after the request**. Any file download must therefore happen in the
same run that fetched the URL — a URL stored now and fetched later will fail.
Do not persist these URLs and expect them to work. They are visible with
`--format full`.

This tool does not download files today; it is read-only and prints parsed data.

## Scope

Read-only by intent. No mutations, no integrations, no webhook server. See
`integrations/README.md` before adding anything that writes to another system.
