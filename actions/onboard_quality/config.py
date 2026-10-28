"""The onboard-quality-check tiers and limits, in ONE place so they can be edited.

A code the audit emits is classified by `tier(code)`:
  * ALERT_CODES                 posted to Slack when NEW, counted every run;
  * COUNT_CODES / COUNT_PREFIXES  counted (shown in the Monday digest), never alerted;
  * IGNORE_CODES / IGNORE_PREFIXES  dropped;
  * anything else               dropped too (a code that is in no list is not part of this check).
The alert list wins over the prefixes, so `child_end_famly_later_over_31_days` is an alert while the other
`child_end_*` sub-codes are only counted.
"""

ALERT_CODES = (
    "child_missing",                      # only for stages that should have a child: see CHILD_NOT_YET_EXPECTED_STAGES
    "contact_missing",
    "contact_relation_missing",
    "bill_payer_missing",
    "bill_payer_ambiguous",
    "bill_payer_contact_missing",
    "bill_payer_no_contact",
    "bill_payer_child_has_no_live_payer",
    "bill_payer_not_emergency",
    "bill_payer_share_zero",
    "bill_payer_other_payer_billing",
    "bill_payer_recipient_missing",       # the payer's contact is not an invoice recipient: no invoices for the parent
    "double_billing_risk",
    "child_end_famly_later_over_31_days",
    "child_end_famly_earlier_over_31_days",
    "child_end_famly_open_ended",
    "plan_missing",
)

COUNT_CODES = ("plan_fee_over_10_percent", "child_start_differs", "child_end_differs")
COUNT_PREFIXES = ("child_start_", "child_end_")          # the other start/end sub-codes

IGNORE_CODES = ("bill_payer_recipient_present", "child_group_differs", "child_name_not_comparable", "plan_input_sessions_missing",
                "plan_not_derivable", "no_hubspot_reference")
IGNORE_PREFIXES = ("child_name_", "name_")

# Operations stages (labels) whose deals are not expected to have a child in Famly yet: `child_missing`
# is not an alert for them. Onboarding creates the child from Deposit Pending.
CHILD_NOT_YET_EXPECTED_STAGES = ("Deposit Pending",)

# Left Nursery deals are in scope only while their end_date is within this many days (final invoices).
LEFT_NURSERY_STAGE = "Left Nursery"
LEFT_NURSERY_WINDOW_DAYS = 45

# A reference file (the five generated ones) older than this many days adds one Slack line to the run.
REFERENCE_MAX_AGE_DAYS = 3

MAX_IDS_PER_CODE = 25
KEEP_REPORTS = 8
STATE_FILE_NAME = "onboard_quality_state.json"      # beside ONBOARD_STORE_PATH
REPORTS_DIR = "reports"
ALERT, COUNT, IGNORE = "alert", "count", "ignore"


def tier(code: str) -> str:
    if code in ALERT_CODES:
        return ALERT
    if code in IGNORE_CODES or code.startswith(IGNORE_PREFIXES):
        return IGNORE
    if code in COUNT_CODES or code.startswith(COUNT_PREFIXES):
        return COUNT
    return IGNORE
