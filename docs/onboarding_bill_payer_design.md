# Onboarding phase 3: the bill payer stage

Status: BUILT, 2026-10-19, with fakes only. Nothing has run live. `ONBOARD_BILL_PAYER=off`
is the default and leaves every earlier behaviour unchanged.

## Settled facts (live probes; not up for debate here)

- `billPayers.create` accepts inline children `[{childId, share: {multiplier}}]`: one mutation
  creates and attaches, and returns `billPayerId`.
- `billPayers.addChildren` with `{childId, billPayerId, share: {multiplier}}` works on an existing
  payer and returns `[{billPayerId, childId, share{multiplier}}]`.
- Scans (`Query.billPayers.listBySiteIds`) show a new record at T+0. `includeDeleted` defaults to
  TRUE and is always passed explicitly; the stage passes FALSE and also ignores any payer with
  `deletedAt`. Deleted children do not appear in a payer's children.
- An HTTP error or `errors` in the body does NOT mean the mutation did not run. The "Transient"
  display value means nothing. Never retry.
- Existing families use multiplier 1.0 or 0.0 only. Existing shares are never edited.
- The token cannot read `socialSecurityNumber`; it, `accountNumber` and `sortCode` are never selected.
- Bill payers cannot be hard deleted, only archived. The pipeline never deletes, archives or edits one.
- `externalId` is used on existing payers for something else: it is not read or written.
- A site has about 110 to 135 live payers: 3 pages, about 3 seconds a scan.

## Switches

- `ONBOARD_BILL_PAYER=off|on` (default off). Off: no bill payer read or write, no plan section.
- On, `ONBOARD_MODE=dry`: the plan and the Slack message show the decision; nothing is written.
- On, `ONBOARD_MODE=live`: writes only for allowed deal ids, after Gate A, after the child and
  contacts steps. A planned create or attach counts as a Famly write, so it needs Gate A.
- `ONBOARD_BILL_PAYER_DETAIL_SELECTION` (optional): the name and phone fields of a BillPayer to add to
  the scan (for example `name phoneNumbers { value }`). Without it nothing is compared and
  `bill_payer_differs` can never be raised. The field names are not recorded in the repo; take them from
  `probe-bill-payers schema`.

## The decision

The bill payer is the deal's contact carrying the Bill Payer label (typeId 91; phase 2 already
computes `isBillPayer`). The scan covers the child's site only, live payers only, all pages, and
matches on the CANONICAL email: trim, lower case, googlemail.com = gmail.com, plus tags kept.

| Situation | Result |
|---|---|
| no Bill Payer contact, or more than one | flag `bill_payer_no_contact` |
| contact has no email | flag `bill_payer_no_email` (never create without one) |
| scan fails (error, page cap, deadline) | flag `bill_payer_scan_failed`, no write |
| 0 live matches | create (a first and last name is needed, else `contact_name_incomplete`) |
| exactly 1 live match | attach |
| 2 or more | flag `bill_payer_ambiguous`, no write |

With the child id known (the child exists at plan time, or at write time): the matched payer already
lists it, `bill_payer_present` (no write); a DIFFERENT payer lists it with a multiplier above 0 (or one
that cannot be read), flag `bill_payer_child_linked_elsewhere`, no write (the double billing guard).

Flags never block the child or the contacts. The stage ends the job `needs_review` when any flag is raised.

## The write

Only after Gate A. The stage re-scans immediately before writing and recomputes; if the decision, the
matched id or the candidate count differs from the approved one it writes nothing and flags
`bill_payer_plan_changed` (the child and contacts stand).

- create: one `billPayers.create`: siteId, name and email from the contact, a phone only if the existing
  normaliser accepts it (else omitted with the existing `phone_unnormalised` warning), no address, no
  externalId, `children [{childId, share {multiplier 1.0}}]`. Selection: `billPayerId` only.
- attach: one `billPayers.addChildren` for the one new child at 1.0, selection built from the return
  type's fields (the probe's `selection_for`, sensitive fields excluded).

The mutation documents are built from the live schema (argument names are not in the repo) and checked by
`assert_stage_mutation`, which refuses anything that deletes, archives, updates, names a site-wide
argument or a sensitive field.

Send once, never retry. The only CLEAN success is an error-free response that names the row. Anything
else (any error, a non-2xx status, a timeout, missing data, a partial response with data AND errors) is
an UNKNOWN outcome: rescan immediately, then once more after 10 seconds. Exactly one live payer with the
canonical email listing the child at 1.0 (and, for an attach, being the approved payer) is a success;
otherwise `bill_payer_write_rejected` (the response said 4xx/errors and nothing exists) or
`bill_payer_outcome_unknown`. A clean success is read back with one scan; a mismatch is
`bill_payer_readback_mismatch`.

Differences: if the matched payer's name (case and whitespace insensitive) or phone (E.164) differs from
the HubSpot contact, `bill_payer_differs` is raised naming FIELD NAMES only. Nothing is overwritten.

## State, the plan and the hash

- Stored: only `bill_payer_id`, in a new `onboard_jobs` column (added by the same concurrent-open-safe
  migration as the earlier columns). No new job status; the bill payer outcome is appended to the job's
  outcomes as `billpayer:<code>`.
- The plan has a `billPayer` section: `status` (`would_create`, `would_attach`, `present`, `flagged`),
  `flag`, `billPayerId`, `matches`. It is part of the digest, so a change re-asks under the existing rules
  and the `plan_unstable` cap. The field names that differ (`differs`) and the contact labels are NOT
  hashed. A flag is also a plan warning, so the Gate A message shows its sentence.
- No email, name or phone is stored, logged, put in Slack or in the jobs table.

## Codes (all have plain-English sentences in `actions/onboard/messages.py`)

`bill_payer_created`, `bill_payer_attached`, `bill_payer_present` (information, shown in the summary
line); `bill_payer_ambiguous`, `bill_payer_no_contact`, `bill_payer_no_email`,
`bill_payer_child_linked_elsewhere`, `bill_payer_plan_changed`, `bill_payer_outcome_unknown`,
`bill_payer_readback_mismatch`, `bill_payer_write_rejected`, `bill_payer_scan_failed` (not done);
`bill_payer_differs` (to check). `contact_name_incomplete` is reused when a create has no name.

## HubSpot

After a successful bill payer step the status becomes `bill_payer_linked` ONLY IF the
`famly_sync_status` dropdown already offers it; otherwise it stays `contacts_linked`. To add in HubSpot:
internal value `bill_payer_linked`, label "Child, contacts and bill payer in Famly".

## What was NOT verified (no live call yet)

- The BillPayerInput phone field (name and shape) is read from the live schema at write time; if there
  is none the phone is simply not sent.
- The BillPayer name and phone field names for the comparison (see the detail selection above).
- That a create/addChildren selection built from the live schema validates (the probe proved it for
  addChildren after the fix).

## First live test plan (one test deal, then a second)

1. Deploy; keep `ONBOARD_MODE=live`, the allowlist and the test list to the ONE test deal; set
   `ONBOARD_BILL_PAYER=on`; restart the worker and gunicorn.
2. DRY first: `ONBOARD_MODE=dry`, enqueue the test deal. Read the Slack message: `Bill payer: would be
   created in Famly`. Confirm the deal's Bill Payer contact email matches NO existing payer.
3. LIVE (create): `ONBOARD_MODE=live`, enqueue, approve. Expect `Bill payer created.`, the job `done`,
   `bill_payer_id` set (`python main.py onboard-status`), and one new payer in Famly with the child at 1.0.
4. A SECOND test deal whose Bill Payer contact has the SAME email: dry (expect `an existing bill payer
   would be linked`), then live. Expect `Child linked to the existing bill payer.` and the first
   child's share untouched.
5. After each run check that no payer was archived, deleted or edited, and clean up test data by hand in
   Famly (bill payers cannot be deleted through the API).
