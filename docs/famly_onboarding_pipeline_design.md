# Famly onboarding pipeline: design (draft for approval)

Status: DRAFT, 2026-10-15 (updated for Drew's answers, section 12). Nothing here is built. Decisions marked DECIDED came from Drew; everything else is a proposal or an open question.

## 1. Purpose

When a deal reaches the onboarding stage in HubSpot, make sure Famly has the right child and the right parent contacts attached to that child, with no manual steps, then hand over to the existing plan queue. Everything is idempotent, resumable and gated, like the plan queue already is.

## 2. What we learned that shapes the design

- Famly creates a separate contact record for each child, even for the same parent with the same email. Live check: 42 emails map to 2+ contact ids, none of them duplicates on one child (40 siblings at the same site, 2 across sites). So a contact only makes sense in the context of a child, and the child must be resolved first.
- Famly has a `CopyContacts` mutation (internal API, `childContacts.copyContacts(childId, contactIds)`), so an existing parent can be attached to a new sibling without being re-entered.
- The public API has `contacts.list(childIds)`, returning each contact's `childIds`, `email`, `lastModifiedAt`, `externalSystemsReferences` and `roleInvitations`. That gives a fast live read of one child's contacts.
- A full roster scan takes about 9 seconds. That is fine inside a queued worker; it only mattered for a synchronous HubSpot call (20 second limit).

## 3. Principles

1. Child first, then contacts. Bill payer is parked and slots in after contacts later.
2. Asynchronous, like `queue_timeline`: HubSpot gets a job id straight back, a worker does the work, Slack receives holds and outcomes.
3. Every write is preceded by a LIVE read to see whether it is already done, and followed by a read-back. Never the cache. The roster cache is for linking only, as its own docstring says.
4. One active job per deal. Jobs resume by read-back after a crash, never by replaying a write.
5. Nothing is guessed. Two matches, a missing id or a mismatch means HOLD for a person, never pick one.
6. Everything has a dry-run mode that reports what it WOULD do. Each phase ships dry-run first.
7. Names never touch our disk, logs or Slack history beyond what the plan preview already shows (see section 7).

## 4. The pipeline

Trigger: deal reaches the onboarding stage and has the data the worker needs. HubSpot sends only `{dealId}` (DECIDED: worker fetches details from HubSpot).

### Stage 0: Intake (synchronous, fast)
- Validate the request, derive the job key from the deal id and stage, insert the job atomically.
- Same deal already has an active job: return it (`already_queued`). A different request for a deal with an active job is refused with a Slack notice.
- Reply with the job id. No Famly calls and no HubSpot calls here.

### Stage 1: Child
1. Read the deal and its associated parents from HubSpot (in memory only).
2. Live search for the child by `urn` (Famly `externalId`) across all sites, including past and future children.
3. Outcomes:
   - exactly one match: use it;
   - none: create the child (see Gate A);
   - two or more: HOLD, report the ids (ids only).
4. After any creation, read the child back by urn and confirm it exists exactly once.
5. Write the child id to the HubSpot deal. This replaces the `urn_lookup` backfill step for new children.

### Stage 2: Contacts, once per parent email on the deal
For each parent:
1. List the child's contacts live (`contacts.list(childIds: [child])`).
2. Email already on this child: done, record the contact id.
3. Otherwise search for that email on any OTHER child:
   - found on one or more: `CopyContacts` the newest copy (by `lastModifiedAt`) onto this child. If copies differ, take the newest and flag it with counts only (open question 4);
   - not found anywhere: create the contact on the child using details from HubSpot.
4. Read back and confirm the contact is on the child exactly once.
5. The contact id goes in the JOB RECORD only (DECIDED). It is not written to HubSpot.
6. Contacts are created with an EMPTY `roles` list, so no login is created and no invitation can be sent. Granting app access (assigning the Parent role with the child as target) is a separate decision, see section 9.

### Stage 3: Bill payer, plans, deposit, invoice, contract
In scope as of 2026-10-15 but NOT yet designed in detail. See section 12 for the phasing and open questions.

### Stage 3b: App access (separate pipeline, DECIDED)
Not part of this job. Parents get the Parent role (child as target) only after the deposit is paid, triggered separately from HubSpot. This job creates contacts with empty `roles` and never assigns one. When that second pipeline is designed, it needs the contact id for the child, which it can find with a live `contacts.list` by email, so we do not need to store ids in HubSpot.

### Stage 4: Hand-off
Mark the deal ready for plan creation. The existing plan queue picks it up from the child id on the deal.

## 5. State

Job states, matching the plan queue where possible: `queued, running, held, done, dry_done, failed, cancelled`. The job also records the current stage and a per-step log of `{step, status, famly ids, timestamp}`. No names, emails or phone numbers.

HubSpot gets two deal properties only: `famly_sync_status` (`queued, child_ready, contacts_linked, needs_attention`) and `famly_sync_note` (a short reason, no personal data). The child id keeps its existing property.

## 6. Gates and safety

- **Gate A, child creation: requires a Slack Approve/Reject click (DECIDED), to be automated later** once the system is trusted. Reuse the plan-write approval flow (signed Slack interactivity, idempotent clicks, atomic state changes). Build it with a setting so removing the gate is a config change, not a rewrite.
- **Gate B, contact copy and create:** automatic, but only for deals on an allowlist during rollout (the same idea as `COMMIT_ALLOWED_CHILD_IDS`), plus a daily cap and a pause switch, like the queue.
- Kill switches: `ONBOARD_MODE=off|dry|live`, a pause command, a consecutive-failure breaker.
- Every Famly write goes through one small module so the guards cannot be bypassed.

## 7. Names and personal data

Creating a child or contact needs names, which `CLAUDE.md` currently forbids us to store. The design keeps the rule in spirit: names are read from HubSpot at run time, held in memory for the one API call, and never written to the job table, logs or audit trail. `CLAUDE.md` needs a one line amendment saying so. The Slack approval for child creation will show a name, as the plan preview already does by pulling it live from Famly; confirm that is acceptable.

## 8. Failure handling

| Failure | Behaviour |
|---|---|
| Famly or HubSpot unreachable | Retry with backoff, then `failed` and a Slack notice. Safe to re-send: every step re-reads first. |
| Crash mid-step | On restart, resume by live read-back to see whether the write landed. Never replay. |
| Ambiguity (2+ children for a urn, odd contact state) | HOLD, Slack message with ids, deal status `needs_attention`. |
| Parent on the deal has no email | HOLD. |
| Gate A rejected | Job `cancelled`, deal marked `needs_attention`. |

## 9. Discovery results (2026-10-15) and remaining unknowns

Sources: the public API docs (docs.famly.co) and read-only introspection run on the droplet. Both the public and internal APIs use the organisation token held in `.env`, so there is no separate scoped token to gain.

Correction: an earlier summary of the docs said `ChildInput` has no `externalId`. That was wrong. `ChildInput` has `externalId`, `externalSystemsReferences` (`ForeignIdReferenceInput`: `foreignId`, `system`), `primaryHomeLanguage` and `diagnosedConditions`. The urn can be set at creation.

API facts:

- Mutations: `children.create/update/delete`, `contacts.create/update`, `billPayers.create/update/addChildren`, `inquiries.create`. `isTestChild` exists on child creation.
- `ChildInput`: required `name`, `currentGroupId`, `records`, `sensitiveRecords`, `sitesRelation`; optional `gender`, `birthday`, `isTestChild`, `primaryHomeLanguage`, `externalId`, `diagnosedConditions`, `externalSystemsReferences`.
- `SiteRelationInput`: `siteId` (InstitutionId, required), `firstDay` (required), `lastDay` (optional).
- `ContactInput`: required `name`, `phoneNumbers`, `records`, `childIds`, `roles`; optional `email`, `address`, `emergencyContact`. `ContactUpdate` has no `childIds`, so the public API cannot attach an existing contact to another child (only create with `childIds`, or the internal `CopyContacts`).
- `PhoneInput`: `value` (PhoneNumber), `phoneType` (MOBILE, HOME, WORK).
- `RoleAssignmentInput`: `roleId` and `targetId` (UniqueId; the child, as observed).
- `RecordInput`: `key` and `value`, both strings. Child record keys (BasicChildRecordKey): EXTRA_INFO, LANGUAGE, NATIONALITY, BIRTHPLACE. Contact record keys: TITLE, OCCUPATION, RELATION, EXTRA_INFO, PLACE_OF_WORK. Sensitive child record keys (SensitiveChildRecordKey): ALLERGY, SPECIAL_DIETARY_CONSIDERATIONS, SPECIAL_NOTES, VACCINES, TOLERATES_PENICILLIN, DOCTOR_* and DENTIST_* contact fields, ETHNICITY, RACE, RELIGION. `Gender`: FEMALE, MALE, DIVERSE. Roles: RoleTarget includes CHILD; invitation verification methods are DATE_OF_BIRTH, FULL_NAME, NO_VERIFICATION.
- `NameInput` is a custom scalar; its accepted format is not known.
- No lookup by `externalId` or email; list scans only. Rate limits, idempotency and webhooks are not documented.

Behaviour tested by Drew:

- Contacts are separate records per child, but with the same email, assigning a role to a contact adds access to that child on the parent's one login. A first role for an email gives access to that child only; a second child's role on the same email adds it.
- Creating a contact with no roles should not create a login or send an invitation (to confirm with an empty `roles` list).

Consequences:

- The urn can be set with `externalId`. `externalSystemsReferences` can also carry the HubSpot deal id (`system` HUBSPOT) on the child, giving us a second key for idempotency and for tracing a child back to its deal.
- App access is a separate step from creating the contact. Assigning the Parent role with the child as target grants access (and may invite).
- Whether to copy or create sibling contacts is a data question, not a login question: both give the same login behaviour because the email matches.

Internal endpoints found by Drew (same token):

- `GET https://app.famly.co/api/v2/children/{childId}/summary` returns, in one call, the institution, the child (name, birthday, start and end dates, `isTestChild`, group, `onApp`), the group, and every contact ("relation") with `email`, phone fields, `emergencyContact`, `placeOfWork`, `occupation`, `status`, `loginId`, `onApp` and `roles2`. This is the ideal live existence check and read-back after a write.
- The `ChildrenList` GraphQL query (`childrenList(siteId, limit, cursor, filter, sortOrder)`, filter `enrollmentStatuses: [CURRENT, UPCOMING]`) lists a site's children with their contacts and role ids, 100 per page. Documented on the Famly Notes page in Notion. The full `ChildrenListFilter` fields are not yet known (introspection).
- A test child already exists (`isTestChild`, "AATest ProbeChild") with two test contacts. The one with the Parent role has a login and `onApp: true`; the one with no roles has `loginId` empty and `onApp: false`. That confirms a contact with no roles has no login.
- The contact with the Parent role on that test child is the same contact that appeared as ambiguous in the earlier email lookup, so the "42 emails with 2+ contact ids" figure includes some test data. It does not change the conclusion that siblings get separate contact records.

Remaining unknowns:

1. `NameInput` format: Drew supplied the docs shape (`firstName`, `middleName`, `lastName`, `fullName`, `shortName`). Confirm which fields are required on input using the test child.
2. Whether `contacts.create` accepts an empty `roles` list and then sends nothing.
3. Whether `CopyContacts` copies role assignments (and so grants access or invites).
4. Where `currentGroupId` and the site come from for a new child (the groups and institution catalogues already in the repo are the likely source).
5. A HubSpot private app token on the server and its scopes.
6. DECIDED: all contacts associated to the deal count as parents for now (no father/mother label exists). Risk: a non-parent associated contact would be created. Consider an association label later.
7. (Decided) Parents get app access after the deposit is paid, as a separate trigger. Open: what that trigger is and what it sends to Famly.

## 9b. How HubSpot holds the data today (schema only, read 2026-10-15)

Pipelines that matter: **Sales** (Enquiry, Tour Stage, Decision, Registered, Contract sent, Contract Signed) and **Operations** (Deposit Pending, Deposit Paid, EYMan Onboarded, Settling In Booked, Active In School, Frozen Account, Gave Notice, Left Nursery).

- **Each parent is their own HubSpot contact; each deal is one child** (Drew, 2026-10-15). The contact schema still carries "Parent / Carer 1 and 2" and child properties from the EY Man registration form, including `child_urn`, `child_urn_2` and per-parent phone, relationship, employer, address and emergency contact fields, so some data may sit on a parent's own contact. Which contacts count as the parents of a deal (all associated contacts, or an association label) and which of those properties is authoritative per parent is still to be confirmed.
- **The deal is one child.** It has `child_urn`, child first, last and full name, `child_date_of_birth`, `famly_childid`, `famly_plan_uploaded`, `room`, and start dates.
- Mapping looks natural: HubSpot phone types map to Famly MOBILE/HOME/WORK, relationship maps to the `RELATION` contact record, employer to `PLACE_OF_WORK`, emergency contact to `emergencyContact`, nationality to the `NATIONALITY` child record, gender to Famly `Gender`.
- Two contact properties `famly_contact_id` and `famly_bill_payer_id` already exist (from the first design). Under the decision to keep contact ids in the job record they are unused; leave them until the pipeline ships, then retire.
- The contact also holds sensitive data (national insurance numbers, dates of birth, photos, birth certificate). The worker must request an explicit allow-list of properties only, never "all properties", and must never log or store them.

Likely triggers (to confirm): deal enters Operations **Deposit Pending** (contract signed): create child and contacts. Deal enters **Deposit Paid**: app access, as a separate pipeline.

## 10. Build order

0. Discovery (section 9). Read-only, plus one test child.
1. Foundations: generalise the job table for a second job type, HubSpot client, `CLAUDE.md` amendment, mode switch and kill switches.
2. Child stage, dry-run only: finds the child, reports what it would do. Run against real deals and compare with reality.
3. Child creation behind Gate A on a test child, then allowlisted real children.
4. Contact stage dry-run, then `CopyContacts` and create behind the allowlist.
5. HubSpot workflow, status properties and the hand-off to the plan queue.
6. Bill payer stage.
7. After N clean runs, remove Gate A.

## 11. What happens to the contact_lookup work already committed

The lookup endpoint (commit `6f80938`, local only, not pushed) answers a question we no longer need to ask, so it should not be deployed. The roster cache and its live-fetch helpers are reusable for the worker's reads. The child-aware follow-up prompt is withdrawn.

## 12. Expanded scope and decisions (Drew, 2026-10-15)

DECIDED:
- Trigger: a deal is created in the Operations pipeline (copied from the Sales deal) and lands in **Deposit Pending**.
- All associated contacts are created as parents for now. They are created with EMPTY `roles` (no login, no invite). The Parent role is assigned later by the separate post-deposit pipeline.
- No sensitive records for now (`sensitiveRecords` empty).
- The HubSpot private app is named **Famly_Server_Key**. Both Famly APIs use the org token in `.env`.
- After the deal exists, the server should create in Famly: child, contacts, bill payer, plans; then a deposit; then (invoice for the month before start, containing only the deposit); and upload the contract from HubSpot.

Deposit endpoint (internal): `POST https://app.famly.co/api/v2/deposits/{institutionId}/{childId}/{billPayerId}` with `{"deposits":[{"amount":1500,"date":"2026-10-15","note":"","status":""}]}`.

Proposed phasing (each phase dry-run first, then live behind its own gate):
1. Child + contacts (sections 4 and 10). Zero money, easy to verify and reverse on a test child.
2. Bill payer and plans (plans reuse the existing plan queue).
3. Deposit. Money: needs its own approval gate, an idempotency check (read existing deposits for the child first), and test-child verification.
4. Invoice for the month before the start date, deposit only. Unverified whether Famly's invoice run produces a deposit-only invoice; test on a test child before designing around it.
5. Contract upload from HubSpot. Famly endpoint unknown; capture the request from the Famly UI first.

Open questions:
- Where does the deposit amount and date come from (a deal property, or a fixed 1500)?
- Who is the bill payer, and how is it chosen from the associated contacts? `BillPayerInput` needs `siteId` and `name`; then `billPayers.addChildren`.
- Is the deposits POST idempotent? Assume NOT; a duplicate would double charge. Read the child's existing deposits before posting, and read back after.
- Where is the contract stored in HubSpot (file property, attachment, or e-sign tool) and which Famly endpoint stores it?
- How do `currentGroupId`, site and `firstDay` derive from the deal (`room`, start date)?
- How should invoices be triggered: by our server, or by Famly's own billing run?

HubSpot Famly_Server_Key scopes (minimum): `crm.objects.deals.read`, `crm.objects.deals.write` (status and note properties only), `crm.objects.contacts.read`, `crm.schemas.deals.read`, `crm.schemas.contacts.read`, and `files` only if the contract is fetched from HubSpot Files. No contacts write, no companies, no marketing or settings scopes. Associations need no extra scope when deals and contacts read are both present. Token lives only in the server `.env`.

## 13. Answers received 2026-10-15 (second round)

DECIDED:
- Volume: steady state 3 to 10 new contracts a month, one child at a time. A one-off backfill batch is needed for existing deals not yet in Famly.
- Bill payer: the associated contact carrying the HubSpot **bill payer association label**. Others are parents only.
- Deposit amount: a deal property (name to confirm). Deposit-only invoice: tested by Drew, works if the child has no scheduled days in that month.
- Contract: a HubSpot **quote**. New deals: quote associated to the deal. Older deals: quote sits on the associated Sales deal (deal-to-deal association with the sales label). Scope `crm.objects.quotes.read` added. Quote has `hs_pdf_download_link`, `hs_status`, `hs_quote_esign_status`, `hs_esign_date`, `hs_manually_signed`.
- Group by child age: under 24 months Hatchlings, 24 to 36 months Little Wings, 36 months and over Voyagers. Site from `location_of_interest`, first day from `actual_start_date_1`.
- `ChildrenListFilter` fields: `roomIds`, `enrollmentStatuses`, `ageRanges`, `searchTerm`, `tagIds`. There is no site field in the filter (site is a separate argument). Whether `searchTerm` matches `externalId` is untested.

Issues found:
- `custom_child_age_calculation__yrs_` is a number in YEARS with decimals, not months, and is a calculation relative to a date we do not control. Thresholds are 2.0 and 3.0 years. Group should be decided at the child's START date, from `child_date_of_birth` and `actual_start_date_1`, not from today. Use the property only as a cross-check. Needs Drew's confirmation of the rule.
- `location_of_interest` options are Westminster, City, Canada Water, Shoreditch, Head office, Nannying, Other. Only some map to a Famly site. Anything else, or a value that is not the nursery actually signed, must HOLD. Also it is an enquiry preference and may differ from the contracted site.
- Quote selection: a deal can have several quotes. Rule proposed: only a quote with e-sign complete (or manually signed), and HOLD if there is not exactly one. The PDF link may need a HubSpot token to download; test.
- Backfill: deposit must only be created for deals actually at Deposit Pending. For later stages the deposit is already paid, so creating one would be wrong. Backfill needs a per-stage rule and a dry-run report for the whole batch, with one batch-level Slack approval instead of one per child.
- Bill payers may be shared by siblings, so the bill payer stage needs an existence check (live `billPayers` read, matched by email) before create, then `addChildren`. The audit comparing duplicate bill payers per email was never run.
- Invoice for a future month: not known whether Famly allows creating it before that month starts.

## 14. Decisions, third round (2026-10-15)

DECIDED:
- Room (group) = age at the START date, from `child_date_of_birth` and `actual_start_date_1`, in completed months: under 24 Hatchlings, 24 to under 36 Littlewings, 36 and over Voyagers. Group ids per site come from `reference/groups_catalogue.json` (titles Hatchlings, Littlewings, Voyagers; Shoreditch has none yet, so it HOLDs).
- Site = `location_of_interest` (Drew: it is correct). Westminster, City, Canada Water map to HDWEST, HDCITY, HDCW. Any other value HOLDs.
- Child lookup: the public API has `listByExternalSystemReference(foreignId, system)`. It searches every site, includes past and future children, is not paginated, matches case-insensitively, and may return more than one child (HOLD if so). Plan: every child gets an external system reference `HUBSPOT` = deal id (and optionally `URN` = urn in addition to `externalId`). Lookup order: deal id reference first; for legacy children without one, fall back to the urn (`externalId`) via the existing live roster scan, then add the deal id reference as a gated write.
- Deposit by stage (phase 3, not built yet): Deposit Pending creates an unpaid deposit; later stages create the deposit marked already paid, using the `deposit_amount` deal property; Left Nursery gets NO deposit. Plans are still created for Left Nursery children so past occupancy is visible.
- Invoices: Famly allows an invoice for any month. Drew will supply the create-invoice request when we reach that phase. Drew will also supply the request that lists a child's deposits, to use as the existence check before posting.
- Phase order: 1 child and contacts (dry-run first), then bill payer and plans, deposit, invoice, contract.

Dry-run first build is specified in `/mnt/user-data/outputs/onboard_phase1_prompt.md`.

## 15. HubSpot association labels (Drew, 2026-10-15)

- Bill payer: label "Bill Payer" / inverse "Childcare Account", internal name `bill_payer_childcare_account`, id 91, limits 1-to-1.
- Sales deal link: label "Operations" / inverse "Sales", internal name `sales_operations`, id 138, limits 1-to-many.
- Direction of each label is unconfirmed; phase 1 dry run must read the labels from the v4 API and report which direction carries which.
- CLARIFIED by Drew: each deal has at most one Bill Payer contact, but one contact can be the bill payer ("Childcare Account") on as many deals as they like. Siblings are therefore fine. A deal with no labelled bill payer HOLDs (`bill_payer_unclear`).
- Drew changed the Operations/Sales label to 1-to-1, so an Operations deal has at most one Sales deal.

## 16. Create-time field decisions (Drew, 2026-10-15)

Child (ChildInput):
- name from `child_first_name` and `child_last_name`; birthday from `child_date_of_birth`; currentGroupId derived (age at start; Voyagers if Left Nursery); externalId = `child_urn`; externalSystemsReferences = HUBSPOT deal id (and URN).
- sitesRelation: siteId from `location_of_interest`, firstDay = `actual_start_date_1`, lastDay = `end_date` ("Leave Date") when set. Left Nursery with empty `end_date` HOLDs, because a child with no lastDay counts as current. Every stage: `end_date` before start HOLDs.
- gender: DECIDED "not specified". `Gender` has only FEMALE, MALE, DIVERSE, and gender is optional, so OMIT the field. Do not use DIVERSE.
- No nationality, language, birthplace or extra-info records. No sensitive records. These will be collected from parents by forms after they join.
- isTestChild false (true only for the test child).

Contact (ContactInput, per docs): required `name`, `phoneNumbers`, `records`, `childIds`, `roles`; optional `email`, `address`, `emergencyContact`. Adding a role to a contact without a login creates a login, so `roles` is EMPTY here. Other roles for the later app-access pipeline: Parent d27c12e8-e1bf-4cb6-b10e-def7a83e3350, Parent (limited access) d8632ad6-0b47-4479-8f1d-fcb0477ff4db, Family 2a8c1370-3ea7-40e8-9a60-8773bbe66c5e.
- Fields: first and last name, email, address (street, zip, city, state, country), ONE phone number (mobile preferred, then work; none valid means omit with a warning, not a hold), `emergencyContact` true for the bill payer only, records: RELATION = "Parent". OCCUPATION and EXTRA_INFO later.
- Email comparison treats googlemail.com and gmail.com as the same. Existing children never get auto-created contacts that fail to match (see section 14 follow-ups).

Open: HubSpot source properties for contact name, address and phone (standard contact properties or the `parent_N_*` EY Man fields); `AddressInput` field names; `NameInput` shape; phone number format Famly accepts.

## 17. Probe results, contact create (2026-10-15, test child only)

- `contacts { create(contacts: [ContactInput!]!) }` returns `[Contact!]!`; `contacts { update(...) }` exists with separate lists for names, records, roles, emails, phones, emergencyContacts. No delete in the public schema (test contacts are deleted by hand in Famly).
- `NameInput` ACCEPTS A PLAIN STRING ("First Last"). Objects {firstName,lastName} and {firstName,lastName,fullName} were rejected at variable coercion. So the name sent is a single string; how Famly splits it into first and last is NOT yet verified (3-part names and double-barrelled surnames are the risk).
- `roles: []` was accepted. Login and app state of the created contacts is NOT yet verified (the readback returned null).
- Phone: "+447700900123", "07700 900123" and "+44 7700 900123" all accepted (validation is loose). We normalise to +44 anyway.
- Address country: both "United Kingdom" and "GB" accepted. Match what existing contacts use.
- `RELATION` accepted in records on create; stored value NOT yet verified (readback returned null).
- Errors at variable coercion come back as HTTP 400 with display "Transient". The worker must not retry on that.
- Test contacts created (delete in Famly only after the read-only check): 2396388e-d266-44c8-bde1-cf9472946c1d, 24500501-ec65-42bd-9594-f6a99a1025fd, 6e44cba6-4c39-4121-b3af-28b02b65c66e, 6c329a13-2997-4b11-833e-dc3a043f2b60.

## 18. Probe results, child create, and decisions (2026-10-15)

Probe results (two test children, since to be deleted):
- `children.create` works with birthday as `%Y-%m-%d`, sitesRelation firstDay and lastDay, gender omitted, and both external references (HUBSPOT deal id, URN).
- `listByExternalSystemReference` finds the child by either reference, exact, upper case, lower case or padded with spaces.
- A child with a future start date appears under UPCOMING. A child with a past lastDay appears in none of CURRENT, UPCOMING or the combination; it is only visible with the "withdrawn" filter in the Famly UI. This confirms that Left Nursery children with a past `end_date` are inactive and will not count in rotas.
- Internal summary shows gender "Dreng" (genderId 0) when gender is omitted, while the UI shows the field blank. Drew's view: test children show no gender, so this is the default response, not stored data. Treated as harmless; the create step does not check gender.
- Room move history started on the creation date, not the start date, even for a child who had already left. Drew has since changed the room move logic. NOT yet verified by us; phase 2 read-back asserts the current room move starts on the child's start date and stops the job (`room_move_mismatch`) if not.
- Leaving reason defaults to "Other". Accepted for now.
- Errors at variable coercion return HTTP 400 with display "Transient". Never retry.

Decisions:
- CopyContacts is dropped. Contacts are always created from HubSpot, empty roles. `would_copy` no longer exists in the plan.
- Sibling linking is still needed later. Not designed: we do not yet know how Famly represents siblings or whether the public API can set it. Needs its own probe before any build. Siblings can be found from HubSpot via the deal-to-deal and shared contact associations; Famly sibling data currently comes from batch upload.
- Phase 2 (create child and contacts, queued, Slack Gate A, deal allowlist, test deals only first) is specified in `onboard_phase2_prompt.md`. Out of scope there: bill payer, plans, deposit, invoice, contract, app access, siblings, backfill batching.
