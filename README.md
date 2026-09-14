# famly-tool

A small, read-only Python tool for querying Famly's internal GraphQL API for our
own childcare setting's internal use.

Structure modelled on [jacobbunk/famly-fetch](https://github.com/jacobbunk/famly-fetch):
GraphQL query text lives in `.graphql` files, separate from the code that sends
requests; configuration and the access token come from the environment.

## Layout

```
core/config.py                        settings from environment variables
core/session.py                       attaches the access token to requests
core/client.py                        generic GraphQL client (query-agnostic)
actions/staff_credentials/query.graphql   the query text
actions/staff_credentials/runner.py       runs it, owns its response types
integrations/                         reserved, empty (see its README)
main.py                               CLI dispatcher
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
