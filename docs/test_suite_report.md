# Test suite report, 2026-10-27

Report only. No test was deleted or edited. Counts and test names only; no test data is shown.
Method: one timed run of the whole suite (2207 tests, 123 s, 2 known failures, 1 skip), plus static
analysis of every test file (AST). The numbers marked "heuristic" come from name and body matching and
are good to roughly +/- 10 percent.

## 1. Per test file: count, runtime, what it protects

57 files. Runtime is the sum of the file's tests in the timed run.

| File | Tests | Seconds | What it protects |
|---|---|---|---|
| test_queue_timeline | 180 | 34.10 | The persistent commit queue: one job at a time, daily cap, circuit breaker, held jobs, retries and backoff |
| test_onboard_phase2 | 148 | 19.58 | The onboarding worker: gates, child and contact creates, holds, write-back, Retry, guards, no personal data |
| test_create_timeline | 175 | 13.87 | A starter's plans as a chain: segments, approvals, read-back, final end date |
| test_onboard_bill_payer | 60 | 10.58 | The bill payer stage: decision, send-once, no destructive mutation, unconfirmed-write guard |
| test_delete_digest | 84 | 6.87 | The delete_plans end-of-run Slack digest (chunking, rows, backup folder) |
| test_child_lookup | 69 | 6.45 | The child's name in Slack messages, with a hard 3 s deadline and bare-id fallback |
| test_delete_plans | 127 | 5.74 | The temporary, destructive delete_plans action: allowlist, mode, caps, circuit breaker |
| test_timeline | 37 | 2.88 | Plan timeline segmentation (contiguous, no overlap) |
| test_funded_hours | 61 | 2.65 | Clamping funded hours to what Famly can fund |
| test_onboard_urn | 22 | 2.37 | Child URN allocation, returning-child guard, HubSpot write order |
| test_onboard_recipient_link | 15 | 2.11 | The bill payer contact linked as an invoice recipient |
| test_auto_round | 39 | 1.75 | Automatic rounding of monthly totals |
| test_adjust_approve | 51 | 1.36 | The Adjust and Approve flow |
| test_onboard_webhook | 21 | 1.28 | The `/intake` onboard_enqueue action: auth, stage check, no personal data |
| test_onboard_contact_reference | 9 | 1.21 | HubSpot reference on contacts: stamp, match by reference then email |
| test_live_check | 24 | 1.07 | The live check before a commit |
| test_onboard_contact_relation | 9 | 0.92 | RELATION = Parent on a present contact |
| test_onboard_batch | 20 | 0.81 | Batch onboarding from a file: parsing, scope, eligibility, the file gate |
| test_commit_approval | 23 | 0.71 | The preview store and the Slack Approve path |
| test_onboard_child_only | 11 | 0.65 | Left Nursery is child-only |
| test_onboard_existing_child_name | 6 | 0.59 | A missing name holds only a child about to be created |
| test_plan_flag | 21 | 0.58 | famly_plan_uploaded after a commit, and reconcile |
| test_probe_bill_payers_write | 91 | 0.53 | The bill payer write probe: caps, confirm, send-once |
| test_pull_roster | 38 | 0.52 | The roster pull and the live-existence guard |
| test_onboard_dry_run | 75 | 0.36 | The read-only onboarding dry run: holds, matching, no values |
| test_onboard_messages | 18 | 0.35 | Every emitted code has a plain-English sentence |
| test_onboard_allowlist_switch | 5 | 0.34 | ONBOARD_ENFORCE_ALLOWLIST |
| test_onboard_phone_reasons | 8 | 0.30 | Phone failure reason codes and the read-only phone audit |
| test_onboard_audit | 30 | 0.26 | The read-only onboard-audit: guards, codes, no values |
| test_current_plan_state | 63 | 0.26 | Pricing-period selection |
| test_contact_lookup | 12 | 0.24 | The read-only contact_lookup action |
| test_site_code | 23 | 0.21 | The site_code metadata field |
| test_rounding | 40 | 0.18 | The pure rounding rule |
| test_onboard_status_block | 10 | 0.11 | The Gate A and dry-run status block |
| test_pull_sessions | 47 | 0.11 | The pull-sessions command |
| test_pull_products | 51 | 0.10 | The pull-products command |
| test_product_bookings | 56 | 0.09 | Product bookings for non-funded deals |
| test_probe_bill_payers | 30 | 0.08 | The read-only part of probe-bill-payers |
| test_hubspot_set_plan_uploaded | 14 | 0.08 | The set-plan-uploaded command and the writer's property guard |
| test_urn_lookup | 36 | 0.08 | The TEMPORARY urn_lookup backfill |
| test_display_names | 42 | 0.08 | Readable Slack names from the catalogues |
| test_additional_plan | 12 | 0.07 | The additional_plan action (oldPlanId) |
| test_funding_rules | 27 | 0.06 | funding_rules.json and its resolver (2 known failures) |
| test_pull_institution_defaults | 26 | 0.06 | The pull-institution-defaults command |
| test_probe_child_create | 27 | 0.05 | The ONE-OFF probe-child-create guards |
| test_institution_defaults | 21 | 0.05 | The institution defaults resolver |
| test_session_booking | 13 | 0.04 | Session-booking resolution in the HubSpot flatten |
| test_session_catalogue | 17 | 0.04 | The session catalogue |
| test_slack | 29 | 0.04 | The Slack integration |
| test_pull_groups | 13 | 0.04 | The pull-groups command |
| test_plan_end_date | 26 | 0.03 | The plan end date |
| test_discounts | 56 | 0.02 | The three custom discount slots |
| test_probe_contact_create | 14 | 0.00 | The ONE-OFF probe-contact-create guards |
| test_plan_write_failure_logging | 5 | 0.00 | Famly failure logging in plan_write |
| test_pull_references | 15 | 0.00 | The pull-references combined command |
| test_plan_status | 5 | 0.00 | The plan_status action |

The three biggest files (queue_timeline, onboard_phase2, create_timeline) are 503 tests (23 percent of
the suite) and 67.6 s (55 percent of the runtime).

## 2. Tests that cover deleted or superseded behaviour

Almost nothing is dead. Checked by searching every test for the removed probes, retired codes and removed
commands:

- **The two removed probes** (`probe-contact-reference`, `probe-invoice-recipient`): 0 tests remain (their
  files were deleted with them; no reference to either name is left in `tests/`).
- **`contact_unmatched_on_existing_child`** (retired as a hold): 6 references in 3 tests, all in
  test_onboard_phase2 and all deliberate: the repair-flow test (checks the code only as an info footer),
  the Retry-on-an-old-held-job test (checks the old code still renders and re-plans), and the
  recognition check inside it. Keep: they are the regression guard for the retirement.
- **`room_move_mismatch`** (check removed): 1 test, `ReadBackTests.test_the_room_move_is_not_checked_at_all`.
  It asserts a retired check stays gone. Low value; candidate to delete.
- **`gap_not_offered`** (old checkbox handling): 1 assertion inside
  `WriteBackTests.test_gaps_and_note_are_plain_english_text_and_status_is_one_of_three_values`. Candidate to
  drop the one assertion, not the test.
- **`--allow-new-contacts`**: never built; 0 tests.
- **Parked, not retired:** `summary_start_mismatch` (5 references in 2 files) is parked by decision.
- **One-off and temporary commands still in the repo** (their tests become dead the day the command is
  deleted): probe-bill-payers 121 (91 write, 30 read-only), probe-child-create 27, probe-contact-create 14,
  urn_lookup 36 (the package is marked TEMPORARY, but `fetch_roster` is used by live code, so only its
  web/cli parts can go), delete_plans 211 (127 + 84 digest; destructive and gated, in use until deleted).
  Total 409 tests; 162 of them belong to the three probes, which are marked ONE-OFF.

## 3. Near-duplicates, wording assertions, parametrisation

- **Exact structural duplicates** (same test body, only the literals differ): 36 groups containing 86
  tests; one parametrised test per group would remove 50. Top files: test_display_names 14,
  test_product_bookings 10, test_create_timeline 7, test_adjust_approve 6, test_pull_sessions 6,
  test_delete_digest 5, test_rounding 5, test_timeline 5, test_discounts 4, test_funding_rules 4.
- **Near-duplicates inside a class** (95 percent similar structure, bodies of several lines): 384 tests in
  146 groups; one parametrised test per group would remove 238. Top ten files by tests involved:
  test_create_timeline 33, test_queue_timeline 29, test_timeline 25, test_product_bookings 21,
  test_pull_products 17, test_discounts 17, test_current_plan_state 17, test_display_names 16,
  test_delete_digest 15, test_rounding 14. (This count includes the 36 exact groups above.)
- **Wording-string assertions** (a literal English sentence compared or searched, so a copy change breaks
  it): 242 tests. Top ten: test_create_timeline 31, test_probe_bill_payers_write 24, test_queue_timeline 17,
  test_current_plan_state 13, test_display_names 13, test_onboard_phase2 13, test_adjust_approve 12,
  test_onboard_bill_payer 12, test_delete_digest 10, test_probe_child_create 10. The onboarding tests mostly
  already compare against `messages.lookup` / `messages.state`, which survive a copy change; the ones
  above that do not are in the older create_timeline, queue_timeline, adjust and probe suites.
- **Already parametrised** (use `subTest`): 89 tests.
- **Very small tests** (3 statements or fewer): 809, a sign of enumerated cases (top: create_timeline 54,
  queue_timeline 49, child_lookup 42, delete_plans 39, product_bookings 35).

## 4. The slowest 30 tests, and real-time waits

The slowest 30 take 26.1 s of 123 s (21 percent). 22 tests take over 0.5 s; 385 take over 0.1 s. The
average test in test_queue_timeline is 0.19 s and in test_onboard_phase2 0.13 s, which points at per-test
store setup (a fresh SQLite file and worker per test), not at the assertions.

Slowest first (seconds, file, class.test):
3.01 child_lookup HardDeadlineTests.a_slow_response_falls_back...; 2.88 timeline
PropertyTests.segments_are_contiguous...; 2.41 and 2.04 onboard_bill_payer SendOnceTests (add_children and
create "for every failure type": a loop of full pipeline runs); 1.26 queue_timeline
ConcurrentEnqueueTests.ten_different_children...; 0.83 onboard_contact_reference StampTests.created_contact...;
0.77 onboard_phase2 SearchLagTests; 0.75 and 0.75 delete_digest LargeRunTests (two); 0.73 queue_timeline
DailyCapTests; 0.67 queue_timeline HeldTests; 0.66 create_timeline FinalEndDateReadBackTests; 0.65
child_lookup SlowLookupInTheMessagesTests; 0.65 delete_plans CircuitBreakerTests; 0.64 queue_timeline
RetryTests.429_backs_off; 0.62 queue_timeline FinalEndDateTests; 0.59 queue_timeline CircuitBreakerTests;
0.58 queue_timeline ConcurrentEnqueueTests.busy_store; 0.57 onboard_phase2 WriteBackTests; 0.53 queue_timeline
CleanChainTests; 0.51 onboard_urn WorkerTests; 0.50 queue_timeline DryDoneKeyTests; 0.48 create_timeline
ReadBackRetryTests; 0.47 queue_timeline CleanCheckTests; 0.46 delete_digest ContentTests; 0.46
onboard_bill_payer NoDestructiveMutationTests; 0.43 queue_timeline PauseMidJobTests; 0.41 onboard_phase2
ExistingChildRuleTests.retry_on_an_old_held_job; 0.41 queue_timeline CircuitBreakerTests; 0.40 child_lookup
HardDeadlineTests.deadline_is_total.

Real-time waiting: only the `child_lookup` hard-deadline tests (about 3.0 s + 0.65 s + 0.40 s, they run
the real deadline against a blocked client), two tiny sleeps (`time.sleep(0.02)` in test_adjust_approve and
`time.sleep(0.05)` in test_child_lookup), and threaded tests that really wait (queue_timeline concurrent
enqueue 1.26 s and busy-store 0.58 s, delete_digest, adjust_approve, onboard_urn). Every retry or backoff
suite (queue worker, onboarding lookups, bill payer settle, create_timeline read-back) replaces its sleep
with a recorder, so nothing retries in real time. The time is CPU and setup, not waiting.

## 5. The two known failures (`test_funding_rules.ShippedFileTests`)

- `test_the_shipped_file_matches_the_agreed_rules` pins `reference/funding_rules.json` to a frozen copy of
  the rules (the test's `RULES`).
- `test_the_sites_still_to_be_filled_in_are_hard_errors_today` pins that HDCITY at 30 hours has no funded
  hours, so resolving it must raise.
- Why they fail: the file was updated on 2026-10-14 ("Update funding_rules.json") to fill in the sites
  that were still empty. Three keys now differ from the frozen copy: HDCITY `fundedWeeklyHours` (set in
  the file, None in the test), HDCITY `maxFundedMinutes` (set in both, different) and HDWEST
  `fundedWeeklyHours` (set in the file, None in the test). The second test fails because HDCITY now
  resolves instead of raising. The tests are correct about the design (a data change must be a deliberate
  test change); they were just never updated.
- Is fixing small? Yes: update `RULES` to the new file (three keys) and replace the "hard errors today"
  test with a check on a site that really is still empty, or drop it. About 10 lines. Not done.

## 6. Proposed KEEP LIST (never remove)

Heuristic match by name and body, 512 tests (23 percent of the suite) fall in at least one category.
Counts per category (a test can be in several); the anchor classes are named so a reviewer can protect
them by name.

| Category | Tests (heuristic) | Anchors |
|---|---|---|
| Read-only paths never send a mutation (audit, dry run, probes, pulls) | 40 | `test_onboard_audit.ReadOnlyGuardTests`, `test_probe_bill_payers.ReadOnlyTests`, the FakeHubSpot / FakeFamlyClient write traps in test_onboard_dry_run, `test_pull_roster.ExistenceCheckGuardTests` |
| Mutations sent once, never retried | 94 | `test_onboard_bill_payer.SendOnceTests`, `test_probe_bill_payers_write.SendOnceTests`, the onboard_phase2 create-once and relation-once tests, `test_onboard_recipient_link` (add once), `test_plan_flag` (one PATCH) |
| Unknown outcomes settled by a read, never a resend | 161 | onboard_phase2 `SearchLagTests`, `ContactLagTests`, `UnconfirmedContactGuardTests`, bill payer `UnconfirmedPayerGuardTests`, recipient-link settle tests |
| Plan hash, re-approval, plan_changed | 29 | the Gate A tests in onboard_phase2, `test_onboard_status_block` (hash covers every state), the existing-child repair tests |
| No personal data in output, logs, Slack, job rows | 43 | the PII scans in onboard_phase2, webhook (`NoPersonalDataTests`), dry_run, audit, bill_payer, recipient_link, contact_reference, contact_relation, messages |
| Double-billing and duplicate guards | 48 | bill payer `child_linked_elsewhere` and unresolved guards, contact `UnconfirmedContactGuardTests`, `ReturningGuardTests` (URN), duplicate-chain tests |
| Stage-aware scope (Left Nursery child-only) | 38 | all of `test_onboard_child_only`, the child-only cases in dry_run, audit, recipient_link and relation tests |
| Allowlist and mode gates | 104 | `ModeAndAllowlistTests`, `test_onboard_allowlist_switch`, delete_plans `AllowlistTests`, probe guard tests, commit approval guards |
| One active job per deal, idempotency | 25 | onboard_phase2 `QueueTests` (the unique partial index), queue_timeline idempotency and claim tests, commit double-click tests |
| The HubSpot writer's property allowlist | 3 | `test_hubspot_set_plan_uploaded.OnlyThisCommandMayWriteTheProperty` and the onboard_phase2 write-allowlist test; the pin on who may call `write_plan_uploaded` |

Also keep (not matched by the heuristic): the code-to-sentence completeness test in test_onboard_messages;
the live-check tests (`test_live_check`, 24); the commit guards in `test_commit_approval`; the funding
rules resolver tests. Rule for any consolidation: a keep-list test may be merged into a parametrised test
only if every case it asserts stays asserted.

## 7. Proposed removal plan (safest first)

Expected counts are the reduction in test functions; the assertions are kept in every case except step 1.

1. **Delete the superseded checks** (about 2 tests and 1 assertion): the room-move "not checked" test and
   the `gap_not_offered` assertion. Risk: none.
2. **Fold the 36 exact-duplicate groups into parametrised tests** (-50 tests). Risk: very low (same body,
   literals only). Mostly pure-function files: display_names, product_bookings, adjust_approve, pull_sessions,
   rounding, timeline, discounts.
3. **Fold the near-duplicate groups in the pure-function files** (about -110 tests): timeline,
   product_bookings, pull_products, discounts, current_plan_state, display_names, rounding, pull_sessions,
   pull_roster. Risk: low (no I/O; every case stays as a `subTest`). Net saving is mostly files, not time.
4. **Fold the near-duplicate groups in the stateful suites** (about -110 tests): create_timeline,
   queue_timeline, delete_digest, delete_plans, live_check. Risk: medium: these hold keep-list behaviour
   (send-once, settle, idempotency), so review each group against section 6 first and keep one case per
   guarded behaviour.
5. **Delete the three ONE-OFF probes and their tests together** when Drew says they are done
   (about -162: probe-bill-payers 121, probe-child-create 27, probe-contact-create 14). Risk: low if the
   command is removed in the same commit; the guards they test are only for those commands.
6. **Rewrite, not remove, the 242 literal-sentence assertions** to compare against `messages.lookup` or
   the state map (0 tests removed). Risk: low; lowers the cost of copy changes. Do the create_timeline,
   queue_timeline, adjust_approve and probe suites first.
7. **Make the suite faster without removing coverage** (about -40 percent runtime, 0 tests): a shared
   store fixture for queue_timeline, onboard_phase2 and create_timeline (their per-test average is 0.13 to
   0.19 s), fewer iterations in the two bill payer "every failure type" loops (4.4 s together, but they are
   keep-list tests, so reduce the matrix, never the assertion), and a shorter deadline in the one 3 s
   hard-deadline test (the same test already has a 0.3 s variant). Risk: low to medium.
8. **Do not touch yet:** delete_plans (211 tests) and urn_lookup (36): the first is a destructive, gated
   action still in use, the second feeds live code. Revisit when either is retired.

Total if steps 1 to 5 are done: about -435 tests (20 percent, 2207 to about 1770), the whole keep list
intact (512 tests), and roughly 10 to 15 percent less runtime; step 7 is where the runtime comes from.

## Note on the working tree

While running the suite I saw five tracked files marked deleted in the working tree (`audit_summary.json`,
`batch1.txt`, `deals.txt`, `dryrun.json`, `probe_invoice_recipient_error.json`). They were added by the
commit "pushing misc files to github" at 14:32 and are not related to any test or to this report. The probe
error file was meant never to be committed (it holds a raw Famly response and request); that is for Drew to
look at.
