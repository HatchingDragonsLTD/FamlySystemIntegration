"""Tests for the onboarding pipeline PHASE 1 dry run (`onboard-dry-run`).

Run from the project root:

    python -m unittest tests.test_onboard_dry_run -q

Fakes only: no live Famly, no live HubSpot. The fake HubSpot raises on any
write-shaped call, and the fake Famly client raises on any query that is not a
read. Personal-looking values (names, emails, phone numbers, dates of birth)
are planted in the fakes and must never appear in any output or log line.
"""

import json
import logging
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from actions.onboard_dry_run import derive, links, runner
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.urn_lookup import runner as urn_runner
from core import hubspot
from core.client import GraphQLError
from core.hubspot import HubSpotClient, HubSpotError

WEST_ID, CITY_ID, CW_ID = "site-west", "site-city", "site-cw"

# Planted personal values. None may ever be printed or logged.
PLANTED = [
    "Quillfeather",
    "Zara",
    "parent.one@example.org",
    "parent.two@example.org",
    "07700900123",
    "2024-01-15",
]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeHubSpot:
    """Read methods only; every write-shaped method raises."""

    def __init__(self):
        self.deals = {"D1": make_deal()}
        self.stages = {"s-pending": ("Operations", "Deposit Pending")}
        self.labels = {
            ("deal", "contact"): [
                {"typeId": 91, "label": "Bill Payer", "category": "USER_DEFINED"},
                {"typeId": 3, "label": None, "category": "HUBSPOT_DEFINED"},
            ],
            ("contact", "deal"): [
                {"typeId": 92, "label": "Childcare Account", "category": "USER_DEFINED"}
            ],
            ("deal", "deal"): [
                {"typeId": 138, "label": "Operations", "category": "USER_DEFINED"},
                {"typeId": 139, "label": "Sales", "category": "USER_DEFINED"},
            ],
        }
        self.assoc = {
            ("deals", "D1", "contacts"): [
                {"id": "c1", "types": [{"typeId": 91, "label": "Bill Payer"}]},
                {"id": "c2", "types": [{"typeId": 3, "label": None}]},
            ],
        }
        self.contacts = {
            "c1": {"email": "parent.one@example.org"},
            "c2": {"email": "parent.two@example.org"},
        }
        self.quotes = {}
        self.calls = []

    def get_deal(self, deal_id):
        self.calls.append("get_deal")
        if deal_id not in self.deals:
            raise HubSpotError("GET failed with HTTP 404", 404)
        return dict(self.deals[deal_id])

    def stage_names(self):
        return dict(self.stages)

    def association_labels(self, from_type, to_type):
        return list(self.labels.get((from_type, to_type), []))

    def associations(self, from_type, from_id, to_type):
        return list(self.assoc.get((from_type, from_id, to_type), []))

    def get_contact(self, contact_id):
        return dict(self.contacts[contact_id])

    def get_quote(self, quote_id):
        return dict(self.quotes[quote_id])

    def _forbidden(self, *args, **kwargs):
        raise AssertionError("write attempted against HubSpot")

    post = put = patch = delete = create = update = write = _forbidden


class FakeFamlyClient:
    """The public-API GraphQL client, serving canned reads. Raises on anything
    that is not a plain query."""

    def __init__(self):
        self.ext_refs = {}  # deal id -> [child ids]
        self.child_contacts = {}  # child id -> [(contact id, email)]
        self.scan = []  # [{"id": child id, "contacts": [node, ...]}]
        self.urn_children = []  # [(child id, externalId)]
        self.site_children = []  # [(child id, birthday, full name)]
        self.error = None
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        if "mutation" in text.lower():
            raise AssertionError(f"write attempted against Famly: {operation_name}")
        self.calls.append(operation_name)
        if self.error is not None:
            raise self.error

        if operation_name == "OnboardChildByExternalRef":
            ids = self.ext_refs.get(variables["foreignId"], [])
            return {"data": {"children": {"listByExternalSystemReference": {
                "result": [{"id": i} for i in ids]}}}}
        if operation_name == "OnboardChildContacts":
            rows = self.child_contacts.get(variables["childIds"][0], [])
            return {"data": {"contacts": {"list": {
                "result": [{"id": i, "email": e} for i, e in rows], "next": None}}}}
        if operation_name == "OnboardContactsScan":
            return {"data": {"children": {"listBySiteIds": {"result": self.scan, "next": None}}}}
        if operation_name == "OnboardSiteChildren":
            result = [{"id": i, "birthday": b, "name": {"fullName": n}}
                      for i, b, n in self.site_children]
            return {"data": {"children": {"listBySiteIds": {"result": result, "next": None}}}}
        if operation_name == urn_runner.OPERATION_NAME:
            result = [
                {"id": i, "externalId": u, "name": {"fullName": "Zara Quillfeather"}}
                for i, u in self.urn_children
            ]
            return {"data": {"children": {"listBySiteIds": {"result": result, "next": None}}}}
        raise AssertionError(f"unexpected operation {operation_name}")


def make_deal(**over):
    deal = {
        "dealname": "Zara Quillfeather",
        "dealstage": "s-pending",
        "pipeline": "ops",
        "child_urn": "URN-1",
        "child_date_of_birth": "2024-01-15",
        "actual_start_date_1": "2026-01-15",
        "location_of_interest": "Westminster",
        "deposit_amount": "1500",
        "famly_childid": "",
        "child_first_name": "Zara",
        "child_last_name": "Quillfeather",
    }
    deal.update(over)
    return deal


def scan_contact(contact_id, email, modified="2026-01-01T00:00:00Z", name="Zara Quillfeather",
                 phone="07700900123"):
    return {
        "id": contact_id,
        "email": email,
        "lastModifiedAt": modified,
        "name": {"fullName": name},
        "phoneNumbers": [{"value": phone}],
    }


class DryRunTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "catalogue.json").write_text(json.dumps({"sites": {
            "HDWEST": {"institutionId": WEST_ID},
            "HDCITY": {"institutionId": CITY_ID},
            "HDCW": {"institutionId": CW_ID},
        }}), encoding="utf-8")
        titles = {"Hatchlings": "h", "Littlewings": "l", "Voyagers": "v"}
        groups = {code: {"groups": {f"{code}-{t[0]}": title for title, t in titles.items()}}
                  for code in ("HDWEST", "HDCITY", "HDCW")}
        (root / "groups.json").write_text(json.dumps({"institutions": groups}), encoding="utf-8")

        env = mock.patch.dict(os.environ, {
            "FAMLY_CATALOGUE_FILE": str(root / "catalogue.json"),
            "GROUPS_CATALOGUE_FILE": str(root / "groups.json"),
        })
        env.start()
        self.addCleanup(env.stop)

        self.hub = FakeHubSpot()
        self.famly = FakeFamlyClient()

        # Capture every log record: nothing personal may appear in them.
        self.records = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record.getMessage())
        root_logger = logging.getLogger()
        self._old_level = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        self.addCleanup(root_logger.removeHandler, handler)
        self.addCleanup(root_logger.setLevel, self._old_level)

    def dry(self):
        return runner.DryRun(self.hub, FamlyReads(self.famly, site_ids=[WEST_ID]))

    def plan(self, deal_id="D1"):
        return self.dry().plan_deal(deal_id)

    def reasons(self, plan):
        return [h["reason"] for h in plan["holds"]]


# --------------------------------------------------------------------------- #
# Group derivation: exact at every boundary (wrong-room risk).
# --------------------------------------------------------------------------- #
class GroupDerivationTests(unittest.TestCase):
    def test_room_boundaries_at_23_24_35_36_months(self):
        cases = [
            ("2024-01-15", "2026-01-14", 23, "Hatchlings"),
            ("2024-01-15", "2026-01-15", 24, "Littlewings"),
            ("2023-01-15", "2025-12-14", 34, "Littlewings"),
            ("2023-01-15", "2025-12-15", 35, "Littlewings"),
            ("2023-01-15", "2026-01-14", 35, "Littlewings"),
            ("2023-01-15", "2026-01-15", 36, "Voyagers"),
            ("2025-06-10", "2025-06-10", 0, "Hatchlings"),
            ("2020-03-01", "2026-03-01", 72, "Voyagers"),
        ]
        for dob, start, months, title in cases:
            with self.subTest(dob=dob, start=start):
                self.assertEqual(
                    derive.completed_months(date.fromisoformat(dob), date.fromisoformat(start)),
                    months,
                )
                self.assertEqual(derive.group_title(dob, start), title)

    def test_month_end_birth_days_clamp_to_the_last_day_of_a_shorter_month(self):
        cases = [
            # Born the 31st: 24 months is 31 Jan, 28 Feb would be 25 months' anniversary.
            ("2024-01-31", "2026-01-30", "Hatchlings"),
            ("2024-01-31", "2026-01-31", "Littlewings"),
            # 29, 30 and 31-day months: born 31 Aug, 22 months is 30 Jun.
            ("2024-08-31", "2026-06-29", "Hatchlings"),
            ("2024-08-31", "2026-06-30", "Hatchlings"),
            # Born 31 Dec 2023: 26 months lands in February; the 28th is the anniversary.
            ("2023-12-31", "2026-02-27", "Littlewings"),
            ("2023-12-31", "2026-02-28", "Littlewings"),
        ]
        for dob, start, title in cases:
            with self.subTest(dob=dob, start=start):
                self.assertEqual(derive.group_title(dob, start), title)
        # The clamp itself, directly:
        self.assertEqual(derive.completed_months(date(2024, 8, 31), date(2026, 6, 29)), 21)
        self.assertEqual(derive.completed_months(date(2024, 8, 31), date(2026, 6, 30)), 22)
        self.assertEqual(derive.completed_months(date(2023, 12, 31), date(2026, 2, 27)), 25)
        self.assertEqual(derive.completed_months(date(2023, 12, 31), date(2026, 2, 28)), 26)

    def test_leap_day_births_cross_the_boundary_on_28_february(self):
        # Born 29 Feb 2024: 24 months is 28 Feb 2026 (clamped).
        self.assertEqual(derive.group_title("2024-02-29", "2026-02-27"), "Hatchlings")
        self.assertEqual(derive.group_title("2024-02-29", "2026-02-28"), "Littlewings")
        # Born 29 Feb 2020: 36 months is 28 Feb 2023.
        self.assertEqual(derive.group_title("2020-02-29", "2023-02-27"), "Littlewings")
        self.assertEqual(derive.group_title("2020-02-29", "2023-02-28"), "Voyagers")
        # A leap day start date is an ordinary day.
        self.assertEqual(derive.completed_months(date(2024, 1, 29), date(2028, 2, 29)), 49)

    def test_hubspot_date_formats_all_parse(self):
        # 2024-01-15 and 2026-01-15 as epoch milliseconds (midnight UTC) and ISO datetimes.
        self.assertEqual(derive.group_title("1705276800000", "1768435200000"), "Littlewings")
        self.assertEqual(
            derive.group_title("2024-01-15T00:00:00.000Z", "2026-01-14T00:00:00Z"), "Hatchlings"
        )

    def test_missing_invalid_or_inconsistent_dates_hold(self):
        for dob, start, reason in [
            (None, "2026-01-15", derive.DOB_MISSING),
            ("", "2026-01-15", derive.DOB_MISSING),
            ("2024-01-15", None, derive.START_DATE_MISSING),
            ("2024-01-15", "  ", derive.START_DATE_MISSING),
            ("not-a-date", "2026-01-15", derive.DATE_INVALID),
            ("2024-01-15", "2026-13-45", derive.DATE_INVALID),
            ("2026-01-16", "2026-01-15", derive.DATES_INCONSISTENT),
        ]:
            with self.subTest(dob=dob, start=start):
                with self.assertRaises(derive.Hold) as ctx:
                    derive.group_title(dob, start)
                self.assertEqual(ctx.exception.reason, reason)


# --------------------------------------------------------------------------- #
# Site and group id lookup.
# --------------------------------------------------------------------------- #
class SiteAndGroupIdTests(DryRunTestCase):
    def test_the_three_nurseries_map_and_everything_else_holds(self):
        for value, code in [("Westminster", "HDWEST"), ("City", "HDCITY"),
                            ("Canada Water", "HDCW"), ("  canada WATER ", "HDCW")]:
            with self.subTest(value=value):
                self.assertEqual(derive.site_for_location(value)[0], code)
        for value in ["Shoreditch", "Head office", "Nannying", "Other", "", "  ", None, 7,
                      "Westminster Bridge"]:
            with self.subTest(value=value):
                with self.assertRaises(derive.Hold) as ctx:
                    derive.site_for_location(value)
                self.assertEqual(ctx.exception.reason, derive.SITE_UNMAPPED)

    def test_a_mapped_site_with_no_institution_id_holds(self):
        path = Path(os.environ["FAMLY_CATALOGUE_FILE"])
        path.write_text(json.dumps({"sites": {"HDWEST": {"label": "x"}}}), encoding="utf-8")
        with self.assertRaises(derive.Hold) as ctx:
            derive.site_for_location("Westminster")
        self.assertEqual(ctx.exception.reason, derive.SITE_ID_MISSING)

    def test_group_id_is_looked_up_by_title_per_site(self):
        self.assertEqual(derive.group_id_for("HDWEST", "Littlewings"), "HDWEST-l")
        self.assertEqual(derive.group_id_for("HDCW", "voyagers "), "HDCW-v")

    def test_a_missing_site_title_or_file_is_group_missing(self):
        groups = Path(os.environ["GROUPS_CATALOGUE_FILE"])
        groups.write_text(json.dumps({"institutions": {
            "HDWEST": {"groups": {"g1": "Hatchlings"}},
            "HDCITY": {"groups": {}},
        }}), encoding="utf-8")
        for site, title in [("HDWEST", "Voyagers"), ("HDCITY", "Hatchlings"),
                            ("SHOREDITCH", "Hatchlings")]:
            with self.subTest(site=site, title=title):
                with self.assertRaises(derive.Hold) as ctx:
                    derive.group_id_for(site, title)
                self.assertEqual(ctx.exception.reason, derive.GROUP_MISSING)
        groups.unlink()
        with self.assertRaises(derive.Hold) as ctx:
            derive.group_id_for("HDWEST", "Hatchlings")
        self.assertEqual(ctx.exception.reason, derive.GROUP_MISSING)

    def test_two_groups_with_one_title_is_ambiguous_not_a_guess(self):
        Path(os.environ["GROUPS_CATALOGUE_FILE"]).write_text(json.dumps({"institutions": {
            "HDWEST": {"groups": {"g1": "Hatchlings", "g2": "hatchlings"}}}}), encoding="utf-8")
        with self.assertRaises(derive.Hold) as ctx:
            derive.group_id_for("HDWEST", "Hatchlings")
        self.assertEqual(ctx.exception.reason, derive.GROUP_AMBIGUOUS)


# --------------------------------------------------------------------------- #
# Hold rules, end to end through the planner.
# --------------------------------------------------------------------------- #
class HoldRuleTests(DryRunTestCase):
    def test_a_clean_deal_is_ready_with_site_group_and_stage(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["holds"], [])
        self.assertEqual((plan["siteCode"], plan["groupTitle"]), ("HDWEST", "Littlewings"))
        self.assertEqual(plan["groupId"], "HDWEST-l")
        self.assertEqual(plan["stage"]["depositState"], "unpaid")

    def test_site_and_date_problems_hold_with_the_reason_code(self):
        cases = [
            (dict(location_of_interest="Shoreditch"), derive.SITE_UNMAPPED),
            (dict(location_of_interest=""), derive.SITE_UNMAPPED),
            (dict(child_date_of_birth=""), derive.DOB_MISSING),
            (dict(actual_start_date_1=""), derive.START_DATE_MISSING),
            (dict(child_date_of_birth="2027-01-01"), derive.DATES_INCONSISTENT),
            (dict(actual_start_date_1="garbage"), derive.DATE_INVALID),
        ]
        for over, reason in cases:
            with self.subTest(reason=reason, over=over):
                self.hub.deals["D1"] = make_deal(**over)
                plan = self.plan()
                self.assertEqual(plan["status"], "held")
                self.assertIn(reason, self.reasons(plan))
                self.assertNotIn("groupId", plan)

    def test_a_site_with_no_such_group_holds_group_missing(self):
        Path(os.environ["GROUPS_CATALOGUE_FILE"]).write_text(json.dumps({"institutions": {
            "HDWEST": {"groups": {"g1": "Hatchlings"}}}}), encoding="utf-8")
        plan = self.plan()  # 24 months at start -> Littlewings, which has no group
        self.assertEqual(self.reasons(plan), [derive.GROUP_MISSING])
        self.assertEqual(plan["groupTitle"], "Littlewings")

    def test_an_unmapped_site_never_picks_a_group_id(self):
        self.hub.deals["D1"] = make_deal(location_of_interest="Nannying")
        plan = self.plan()
        self.assertEqual(self.reasons(plan), [derive.SITE_UNMAPPED])
        self.assertNotIn("groupId", plan)
        self.assertNotIn("siteCode", plan)

    def test_a_contact_with_no_email_holds(self):
        self.hub.contacts["c2"] = {"email": ""}
        plan = self.plan()
        self.assertEqual(self.reasons(plan), [runner.CONTACT_NO_EMAIL])
        self.assertEqual(plan["contacts"][1]["status"], runner.NO_EMAIL)

    def test_the_bill_payer_must_be_exactly_one(self):
        # None labelled.
        self.hub.assoc[("deals", "D1", "contacts")] = [
            {"id": "c1", "types": [{"typeId": 3, "label": None}]},
            {"id": "c2", "types": [{"typeId": 3, "label": None}]},
        ]
        plan = self.plan()
        self.assertEqual(self.reasons(plan), [runner.BILL_PAYER_UNCLEAR])
        self.assertEqual(plan["holds"][0]["billPayerCount"], 0)
        # Two labelled.
        self.hub.assoc[("deals", "D1", "contacts")] = [
            {"id": "c1", "types": [{"typeId": 91, "label": "Bill Payer"}]},
            {"id": "c2", "types": [{"typeId": 91, "label": "Bill Payer"}]},
        ]
        plan = self.plan()
        self.assertEqual(plan["holds"][0]["billPayerCount"], 2)
        # No contacts at all.
        self.hub.assoc[("deals", "D1", "contacts")] = []
        self.assertEqual(self.reasons(self.plan()), [runner.BILL_PAYER_UNCLEAR])

    def test_a_missing_deal_is_a_hold_not_a_crash(self):
        plan = self.dry().plan_deal("NOPE")
        self.assertEqual((plan["status"], self.reasons(plan)), ("held", [runner.DEAL_NOT_FOUND]))

    def test_the_stage_is_reported_for_every_deposit_state(self):
        for stage, state in [
            ("Deposit Pending", "unpaid"), ("Deposit Paid", "paid"), ("EYMan Onboarded", "paid"),
            ("Settling In Booked", "paid"), ("Active In School", "paid"),
            ("Frozen Account", "paid"), ("Gave Notice", "paid"), ("Left Nursery", "none"),
            ("Enquiry", "unknown"),
        ]:
            with self.subTest(stage=stage):
                pipeline = "Operations" if stage != "Enquiry" else "Sales"
                self.assertEqual(derive.deposit_state(pipeline, stage), state)
        self.assertEqual(derive.deposit_state("Sales", "Deposit Pending"), "unknown")


# --------------------------------------------------------------------------- #
# Child lookup: live, never the cache.
# --------------------------------------------------------------------------- #
class ChildLookupTests(DryRunTestCase):
    def test_the_deal_id_reference_finds_the_child_without_a_roster_scan(self):
        self.famly.ext_refs["D1"] = ["child-1"]
        child = self.plan()["child"]
        self.assertEqual(child, {"status": "child_found", "famlyChildId": "child-1",
                                 "via": "external_system_reference", "origin": "existing"})
        self.assertNotIn(urn_runner.OPERATION_NAME, self.famly.calls)

    def test_no_reference_falls_back_to_the_urn_live(self):
        self.famly.urn_children = [("child-9", " urn-1 "), ("child-8", "OTHER")]
        child = self.plan()["child"]
        self.assertEqual((child["status"], child["famlyChildId"], child["via"]),
                         ("child_found", "child-9", "urn"))
        self.assertIn(urn_runner.OPERATION_NAME, self.famly.calls)

    def test_no_match_anywhere_would_create(self):
        self.assertEqual(self.plan()["child"], {"status": "child_would_create"})

    def test_two_matches_from_either_step_hold_with_ids(self):
        self.famly.ext_refs["D1"] = ["child-1", "child-2"]
        plan = self.plan()
        self.assertEqual(plan["child"]["status"], "child_ambiguous")
        self.assertEqual(plan["holds"][0], {"reason": "child_ambiguous",
                                            "childIds": ["child-1", "child-2"],
                                            "via": "external_system_reference"})
        self.famly.ext_refs.clear()
        self.famly.urn_children = [("child-3", "URN-1"), ("child-4", "urn-1")]
        plan = self.plan()
        self.assertEqual(plan["holds"][0]["childIds"], ["child-3", "child-4"])
        self.assertEqual(plan["holds"][0]["via"], "urn")


# --------------------------------------------------------------------------- #
# Contacts plan.
# --------------------------------------------------------------------------- #
class ContactPlanTests(DryRunTestCase):
    def test_a_new_child_has_no_copy_logic_every_contact_is_would_create(self):
        # Even if another child carries the same email, nothing is copied.
        self.famly.scan = [{"id": "child-7", "contacts": [
            scan_contact("fc-7", "parent.one@example.org")]}]
        plan = self.plan()  # no child exists yet
        self.assertEqual([c["status"] for c in plan["contacts"]],
                         ["would_create", "would_create"])
        self.assertNotIn("would_copy", json.dumps(plan))
        self.assertNotIn("OnboardContactsScan", self.famly.calls)

    def test_existing_child_parent_on_the_child_is_present(self):
        self.famly.ext_refs["D1"] = ["child-1"]
        self.famly.child_contacts["child-1"] = [("fc-1", "PARENT.ONE@example.org "),
                                                ("fc-2", "parent.two@example.org")]
        plan = self.plan()
        self.assertEqual(plan["holds"], [])
        first = plan["contacts"][0]
        self.assertEqual((first["status"], first["famlyContactId"]), ("present", "fc-1"))
        self.assertEqual(first["labels"], [
            {"typeId": 91, "label": "Bill Payer", "direction": "deal_to_contact"}])

    def test_existing_child_unmatched_parent_is_a_hold_with_candidate_ids_only(self):
        self.famly.ext_refs["D1"] = ["child-1"]
        self.famly.child_contacts["child-1"] = [
            ("fc-1", "parent.one@example.org"),
            ("fc-3", "parent.two@other.com"),   # same local part, different domain
            ("fc-4", "someone.else@example.org"),
        ]
        plan = self.plan()
        second = plan["contacts"][1]
        self.assertEqual(second["status"], "contact_unmatched_on_existing_child")
        self.assertEqual(second["candidateIds"], ["fc-3"])   # ids only: evidence, not a match
        self.assertNotIn("famlyContactId", second)
        self.assertEqual(plan["child"]["origin"], "existing")
        hold = [h for h in plan["holds"] if h["reason"] == "contact_unmatched_on_existing_child"]
        self.assertEqual(hold, [{"reason": "contact_unmatched_on_existing_child",
                                 "hubspotContactId": "c2", "candidateIds": ["fc-3"]}])
        self.assertEqual(plan["status"], "held")
        blob = json.dumps(plan)
        for text in ("example.org", "other.com", "parent.two", "@"):
            self.assertNotIn(text, blob)           # no email, not even a fragment

    def test_a_pipeline_created_child_keeps_would_create_for_an_unmatched_contact(self):
        self.famly.ext_refs["D1"] = ["child-1"]
        self.famly.child_contacts["child-1"] = [("fc-1", "parent.one@example.org")]
        plan = self.dry_with_own_child("child-1")
        self.assertEqual(plan["child"]["origin"], "created_by_pipeline")
        self.assertEqual([c["status"] for c in plan["contacts"]], ["present", "would_create"])
        self.assertEqual(plan["holds"], [])
        # A DIFFERENT child id in the job history is not this child: it existed already.
        other = self.dry_with_own_child("some-other-child")
        self.assertEqual(other["child"]["origin"], "existing")
        self.assertEqual(self.reasons(other), ["contact_unmatched_on_existing_child"])

    def test_a_child_found_only_by_the_urn_is_existing_even_if_the_history_names_it(self):
        from actions.onboard_dry_run.runner import DryRun
        self.famly.urn_children = [("child-9", "URN-1")]      # no HUBSPOT reference at all
        self.famly.child_contacts["child-9"] = [("fc-1", "parent.one@example.org")]
        plan = DryRun(self.hub, FamlyReads(self.famly, site_ids=[WEST_ID])).plan_deal(
            "D1", None, created_child_ids={"child-9"})
        self.assertEqual((plan["child"]["via"], plan["child"]["origin"]), ("urn", "existing"))
        self.assertEqual(self.reasons(plan), ["contact_unmatched_on_existing_child"])

    def dry_with_own_child(self, child_id):
        from actions.onboard_dry_run.runner import DryRun
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[WEST_ID])).plan_deal(
            "D1", None, created_child_ids={child_id})

    def test_googlemail_and_gmail_match_and_nothing_else_is_fuzzy(self):
        from actions.onboard_dry_run.famly_reads import normalise_email
        self.assertEqual(normalise_email(" Ada@GoogleMail.com "), normalise_email("ada@gmail.com"))
        self.assertNotEqual(normalise_email("a.da@gmail.com"), normalise_email("ada@gmail.com"))
        self.assertNotEqual(normalise_email("ada+x@gmail.com"), normalise_email("ada@gmail.com"))
        self.assertNotEqual(normalise_email("ada@gmail.co.uk"), normalise_email("ada@gmail.com"))
        self.assertNotEqual(normalise_email("ada@mygmail.com"), normalise_email("ada@gmail.com"))
        # End to end: present on an existing child, and found as a copy for a new one.
        self.hub.contacts["c1"] = {"email": "Parent.One@googlemail.com"}
        self.famly.ext_refs["D1"] = ["child-1"]
        self.famly.child_contacts["child-1"] = [("fc-1", "parent.one@gmail.com"),
                                                ("fc-2", "parent.two@example.org")]
        self.assertEqual(self.plan()["contacts"][0]["status"], "present")

    def test_two_contacts_with_one_email_on_the_child_hold(self):
        self.famly.ext_refs["D1"] = ["child-1"]
        self.famly.child_contacts["child-1"] = [("fc-1", "parent.one@example.org"),
                                                ("fc-2", "parent.one@example.org")]
        plan = self.plan()
        self.assertIn(runner.CONTACT_AMBIGUOUS_ON_CHILD, self.reasons(plan))
        self.assertEqual(plan["contacts"][0]["status"], "ambiguous_on_child")

    def test_the_bill_payer_is_found_by_label_in_either_direction(self):
        # Defined from the contact's side, with a type id that is NOT the configured 91.
        self.hub.labels[("deal", "contact")] = [{"typeId": 3, "label": None}]
        self.hub.labels[("contact", "deal")] = [
            {"typeId": 777, "label": "Bill Payer", "category": "USER_DEFINED"}]
        self.hub.assoc[("deals", "D1", "contacts")] = [
            {"id": "c1", "types": [{"typeId": 3, "label": None}]},
            {"id": "c2", "types": [{"typeId": 3, "label": None}]},
        ]
        self.hub.assoc[("contacts", "c2", "deals")] = [
            {"id": "D1", "types": [{"typeId": 777, "label": "Bill Payer"}]}]
        self.hub.assoc[("contacts", "c1", "deals")] = [
            {"id": "D1", "types": [{"typeId": 3, "label": None}]}]
        plan = self.plan()
        self.assertEqual([c["isBillPayer"] for c in plan["contacts"]], [False, True])
        self.assertEqual(plan["holds"], [])
        report = links.load_labels(self.hub).report()
        self.assertEqual(report["billPayerDirection"], "contact_to_deal")
        self.assertFalse(report["billPayerTypeIdAsConfigured"])  # drift is reported

    def test_quotes_fall_back_to_the_sales_deal_and_count_exactly_one_signed(self):
        self.hub.assoc[("deals", "D1", "deals")] = [
            {"id": "S1", "types": [{"typeId": 139, "label": "Sales"}]},
            {"id": "X9", "types": [{"typeId": 138, "label": "Operations"}]},
        ]
        self.hub.assoc[("deals", "S1", "quotes")] = [{"id": "q1", "types": []},
                                                      {"id": "q2", "types": []}]
        self.hub.quotes = {"q1": {"hs_quote_esign_status": "COMPLETED"},
                           "q2": {"hs_status": "DRAFT"}}
        quotes = self.plan()["quotes"]
        self.assertEqual((quotes["count"], quotes["exactlyOneSigned"], quotes["source"]),
                         (2, True, "sales_deal"))
        self.hub.quotes["q2"] = {"hs_manually_signed": "true"}
        self.assertFalse(self.plan()["quotes"]["exactlyOneSigned"])


# --------------------------------------------------------------------------- #
# The HubSpot client itself: allow-list, GET only, retry.
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append((url, dict(params or {}), headers))
        return self.responses.pop(0)

    def _forbidden(self, *a, **k):
        raise AssertionError("non-GET request sent to HubSpot")

    post = put = patch = delete = request = _forbidden


class HubSpotClientTests(unittest.TestCase):
    def client(self, responses, sleeps=None):
        session = FakeSession(responses)
        sleeps = sleeps if sleeps is not None else []
        return HubSpotClient("tok", session=session, sleep=sleeps.append), session, sleeps

    def test_every_object_read_sends_an_explicit_allow_list(self):
        client, session, _ = self.client([FakeResponse(200, {"properties": {}})] * 3)
        client.get_deal("1")
        client.get_contact("2")
        client.get_quote("3")
        sent = [params["properties"].split(",") for _, params, _ in session.requests]
        self.assertEqual(sent[0], list(hubspot.DEAL_PROPERTIES))
        self.assertEqual(sent[1], list(hubspot.CONTACT_PROPERTIES))
        for needed in ("email", "firstname", "lastname", "mobilephone", "phone",
                       "address", "city", "zip", "state", "country"):
            self.assertIn(needed, sent[1])
        self.assertEqual(len(sent[1]), 11)  # those ten plus the WhatsApp property
        self.assertEqual(sent[2], list(hubspot.QUOTE_PROPERTIES))
        for props in sent:
            self.assertNotIn("all", [p.lower() for p in props])
        self.assertEqual(session.requests[0][2]["Authorization"], "Bearer tok")

    def test_the_client_has_no_write_path(self):
        client, _, _ = self.client([])
        public = [n for n in dir(client) if not n.startswith("_")]
        for name in public:
            self.assertFalse(
                any(word in name for word in ("create", "update", "delete", "post", "put",
                                              "patch", "write", "set_")), name)

    def test_429_and_5xx_retry_with_backoff_then_succeed(self):
        client, _, sleeps = self.client([
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(503),
            FakeResponse(200, {"properties": {"email": "x"}}),
        ])
        self.assertEqual(client.get_contact("2"), {"email": "x"})
        self.assertEqual(sleeps, [2.0, 2.0])

    def test_persistent_failure_raises_without_echoing_a_body(self):
        client, session, _ = self.client([FakeResponse(500, {"message": "secret value"})] * 5)
        with self.assertRaises(HubSpotError) as ctx:
            client.get_contact("2")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(len(session.requests), hubspot.MAX_ATTEMPTS)

    def test_a_404_is_not_retried(self):
        client, session, sleeps = self.client([FakeResponse(404)])
        with self.assertRaises(HubSpotError) as ctx:
            client.get_deal("9")
        self.assertEqual((ctx.exception.status_code, len(session.requests), sleeps), (404, 1, []))

    def test_associations_follow_paging_and_keep_labels(self):
        client, session, _ = self.client([
            FakeResponse(200, {"results": [{"toObjectId": 1, "associationTypes": [
                {"typeId": 91, "label": "Bill Payer", "category": "USER_DEFINED"}]}],
                "paging": {"next": {"after": "A"}}}),
            FakeResponse(200, {"results": [{"toObjectId": 2, "associationTypes": []}]}),
        ])
        out = client.associations("deals", "D1", "contacts")
        self.assertEqual([o["id"] for o in out], ["1", "2"])
        self.assertEqual(out[0]["types"][0]["label"], "Bill Payer")
        self.assertEqual(session.requests[1][1]["after"], "A")


# --------------------------------------------------------------------------- #
# Whole-run guarantees: read only, no personal data anywhere, counts-only batch.
# --------------------------------------------------------------------------- #
class RunGuaranteeTests(DryRunTestCase):
    def run_all(self, ids=("D1",)):
        return runner.run(list(ids), self.hub, FamlyReads(self.famly, site_ids=[WEST_ID]))

    def test_output_and_logs_contain_no_names_emails_phones_or_birth_dates(self):
        self.famly.ext_refs["D1"] = ["child-1"]
        self.famly.child_contacts["child-1"] = [("fc-1", "parent.one@example.org")]
        self.famly.scan = [{"id": "child-7", "contacts": [
            scan_contact("fc-7", "parent.two@example.org")]}]
        self.famly.urn_children = [("child-5", "URN-1")]
        result = self.run_all()
        blob = json.dumps(result) + "\n".join(self.records)
        for value in PLANTED:
            self.assertNotIn(value.casefold(), blob.casefold(), value)
        self.assertNotIn("@", blob)

    def test_a_run_never_writes(self):
        # FakeHubSpot raises on any write method and FakeFamlyClient on any
        # mutation text; a run that reaches the end touched neither.
        self.famly.ext_refs["D1"] = ["child-1"]
        result = self.run_all()
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["mode"], "dry_run")
        for name in ("post", "put", "patch", "delete", "create", "update", "write"):
            with self.assertRaises(AssertionError):
                getattr(self.hub, name)()

    def test_the_batch_summary_is_counts_only_and_one_bad_deal_does_not_stop_it(self):
        self.hub.deals["D2"] = make_deal(location_of_interest="Shoreditch")
        self.hub.assoc[("deals", "D2", "contacts")] = self.hub.assoc[("deals", "D1", "contacts")]
        result = self.run_all(["D1", "D2", "MISSING", "D1"])  # D1 repeated once
        summary = result["summary"]
        self.assertEqual(summary["deals"], 3)
        self.assertEqual((summary["ready"], summary["held"]), (1, 2))
        self.assertEqual(summary["holdReasons"], {"site_unmapped": 1, "deal_not_found": 1})
        self.assertEqual(summary["children"], {"child_would_create": 2})
        self.assertEqual(summary["depositStates"], {"unpaid": 2})
        flat = json.dumps(summary)
        for ident in ("D1", "D2", "MISSING", "c1"):
            self.assertNotIn(ident, flat)
        self.assertTrue(all(isinstance(v, (int, dict)) for v in summary.values()))

    def test_a_famly_failure_becomes_an_error_plan_with_no_message(self):
        self.famly.error = GraphQLError("boom with parent.one@example.org", [])
        plan = self.plan()
        self.assertEqual(plan["status"], "error")
        self.assertEqual(plan["error"], {"source": "famly", "type": "GraphQLError"})
        self.assertNotIn("example.org", json.dumps(plan))

    def test_the_run_reports_the_association_labels_it_found(self):
        report = self.run_all()["associationLabels"]
        self.assertEqual(report["billPayerDirection"], "deal_to_contact")
        self.assertTrue(report["billPayerTypeIdAsConfigured"])
        self.assertEqual(report["salesTypeIds"], [139])
        self.assertEqual({r["label"] for r in report["dealToDeal"]}, {"Operations", "Sales"})

    def test_the_live_scans_run_once_per_invocation_not_once_per_deal(self):
        self.hub.deals["D2"] = make_deal()
        self.hub.assoc[("deals", "D2", "contacts")] = self.hub.assoc[("deals", "D1", "contacts")]
        self.run_all(["D1", "D2"])
        self.assertEqual(self.famly.calls.count(urn_runner.OPERATION_NAME), 1)


# --------------------------------------------------------------------------- #
# Child safety: recorded id, possible duplicates.
# --------------------------------------------------------------------------- #
class ChildSafetyTests(DryRunTestCase):
    def test_a_recorded_famly_childid_that_does_not_exist_holds(self):
        self.hub.deals["D1"] = make_deal(famly_childid="child-gone")
        self.famly.urn_children = [("child-other", "x")]
        plan = self.plan()
        self.assertEqual(plan["status"], "held")
        self.assertEqual(plan["holds"][0], {"reason": "famly_childid_not_found",
                                            "famlyChildId": "child-gone"})
        self.assertEqual(plan["child"]["status"], "child_unresolved")
        self.assertTrue(all(c["status"] == "child_unresolved" for c in plan["contacts"]))

    def test_a_recorded_childid_that_exists_is_used_when_no_reference_matches(self):
        self.hub.deals["D1"] = make_deal(famly_childid="child-5")
        self.famly.urn_children = [("child-5", "other-urn")]
        plan = self.plan()
        self.assertEqual(plan["child"], {"status": "child_found", "famlyChildId": "child-5",
                                         "via": "famly_childid", "origin": "existing"})
        self.assertNotIn("famly_childid_not_found", self.reasons(plan))

    def test_possible_duplicate_needs_same_site_birth_date_and_name(self):
        self.hub.deals["D1"] = make_deal(child_first_name="Zara", child_last_name="Quillfeather")
        self.famly.site_children = [
            ("dup-1", "2024-01-15", "Quillfeather  ZARA"),   # same words, any order
            ("other-dob", "2024-01-16", "Zara Quillfeather"),
            ("other-name", "2024-01-15", "Mina Brightwater"),
        ]
        plan = self.plan()
        self.assertEqual(plan["child"], {"status": "child_would_create"})
        self.assertEqual(plan["holds"], [{"reason": "child_possible_duplicate",
                                          "childIds": ["dup-1"]}])
        self.assertEqual(plan["status"], "held")

    def duplicate_ids(self, plan):
        found = [h for h in plan["holds"] if h["reason"] == "child_possible_duplicate"]
        return found[0]["childIds"] if found else []

    def test_the_deal_name_can_carry_the_childs_name(self):
        self.hub.deals["D1"] = make_deal(dealname="Zara Quillfeather - Westminster",
                                         child_first_name="Zara", child_last_name="")
        self.famly.site_children = [("dup-1", "2024-01-15", "zara quillfeather")]
        plan = self.plan()
        self.assertEqual(self.duplicate_ids(plan), ["dup-1"])
        self.assertIn("child_name_incomplete", self.reasons(plan))  # last name missing too

    def test_no_name_on_the_deal_means_every_same_birth_date_child_counts(self):
        self.hub.deals["D1"] = make_deal(child_first_name="", child_last_name="")
        self.famly.site_children = [("a", "2024-01-15", "Anyone"), ("b", "2023-01-15", "Else")]
        plan = self.plan()
        self.assertEqual(self.duplicate_ids(plan), ["a"])
        self.assertIn("child_name_unavailable_for_duplicate_check", plan["warnings"])
        self.assertIn("child_name_incomplete", self.reasons(plan))

    def test_creating_a_child_needs_a_name_and_a_birth_date(self):
        for over, reason in [
            (dict(child_first_name=""), "child_name_incomplete"),
            (dict(child_last_name="  "), "child_name_incomplete"),
            (dict(dealstage="s-left", end_date="2026-06-30", child_date_of_birth=""), "dob_missing"),
        ]:
            with self.subTest(over=over):
                self.hub.stages["s-left"] = ("Operations", "Left Nursery")
                self.hub.deals["D1"] = make_deal(**over)
                self.assertIn(reason, self.reasons(self.plan()))
        # An existing child needs neither: nothing is created.
        self.hub.deals["D1"] = make_deal(child_first_name="")
        self.famly.ext_refs["D1"] = ["child-1"]
        self.assertNotIn("child_name_incomplete", self.reasons(self.plan()))

    def test_the_duplicate_search_only_runs_when_a_child_would_be_created(self):
        self.famly.ext_refs["D1"] = ["child-1"]
        self.famly.site_children = [("dup-1", "2024-01-15", "Zara Quillfeather")]
        self.plan()
        self.assertNotIn("OnboardSiteChildren", self.famly.calls)


# --------------------------------------------------------------------------- #
# Left Nursery, start and end dates.
# --------------------------------------------------------------------------- #
class LeftNurseryAndDatesTests(DryRunTestCase):
    def left(self, **over):
        self.hub.stages["s-left"] = ("Operations", "Left Nursery")
        over.setdefault("end_date", "2026-06-30")  # after the 2026-01-15 start
        self.hub.deals["D1"] = make_deal(dealstage="s-left", **over)

    def test_left_nursery_is_voyagers_from_the_catalogue_with_no_age_rule(self):
        self.left(child_date_of_birth="")  # age would be needed for the age rule
        plan = self.plan()
        self.assertEqual((plan["groupTitle"], plan["groupId"]), ("Voyagers", "HDWEST-v"))
        self.assertEqual(plan["groupReason"], "left_nursery_voyagers")
        self.assertEqual(plan["stage"]["depositState"], "none")
        # No age rule hold; only creating the child needs a birth date.
        self.assertEqual(self.reasons(plan), ["dob_missing"])

    def test_stage_matching_ignores_stray_whitespace_and_case(self):
        self.hub.stages["s-left"] = (" operations ", "  LEFT   Nursery ")
        self.hub.deals["D1"] = make_deal(dealstage="s-left", end_date="2026-06-30")
        self.assertEqual(self.plan()["groupReason"], "left_nursery_voyagers")
        self.assertEqual(derive.deposit_state("OPERATIONS ", " deposit   pending"), "unpaid")

    def test_left_nursery_without_an_end_date_holds_as_it_would_look_active(self):
        self.left()
        self.hub.deals["D1"]["end_date"] = ""
        self.assertEqual(self.reasons(self.plan()), ["end_date_missing"])

    def test_left_nursery_without_a_start_date_holds(self):
        self.left(actual_start_date_1="")
        self.assertEqual(self.reasons(self.plan()), ["start_date_missing"])
        self.hub.deals["D1"]["end_date"] = ""
        self.assertEqual(sorted(self.reasons(self.plan())),
                         ["end_date_missing", "start_date_missing"])

    def test_an_end_date_before_the_start_date_is_inconsistent_at_any_stage(self):
        self.hub.deals["D1"] = make_deal(end_date="2025-12-31")  # start is 2026-01-15
        self.assertEqual(self.reasons(self.plan()), ["dates_inconsistent"])
        self.left(end_date="2025-12-31")
        self.assertEqual(self.reasons(self.plan()), ["dates_inconsistent"])
        # An end date equal to the start date is allowed.
        self.hub.deals["D1"]["end_date"] = "2026-01-15"
        self.assertEqual(self.reasons(self.plan()), [])

    def test_an_end_date_in_the_future_warns(self):
        self.left()
        self.hub.deals["D1"]["end_date"] = "2026-12-01"
        with mock.patch.object(derive, "today", return_value=date(2026, 10, 15)):
            plan = self.plan()
            self.assertIn("end_date_in_future", plan["warnings"])
            self.hub.deals["D1"]["end_date"] = "2026-10-15"  # today is not the future
            self.assertNotIn("end_date_in_future", self.plan()["warnings"])

    def test_a_current_child_needs_no_end_date_and_reports_only_presence(self):
        plan = self.plan()
        self.assertEqual(plan["dates"], {"firstDayPresent": True, "lastDayPresent": False})
        self.assertEqual(plan["holds"], [])
        self.hub.deals["D1"]["end_date"] = "2027-03-31"
        plan = self.plan()
        self.assertEqual(plan["dates"], {"firstDayPresent": True, "lastDayPresent": True})
        blob = json.dumps(plan)
        for text in ("2027-03-31", "2026-01-15", "2024-01-15"):
            self.assertNotIn(text, blob)

    def test_an_unparseable_end_date_holds(self):
        self.hub.deals["D1"]["end_date"] = "soon"
        self.assertIn("date_invalid", self.reasons(self.plan()))


# --------------------------------------------------------------------------- #
# Warnings and labels.
# --------------------------------------------------------------------------- #
class WarningAndLabelTests(DryRunTestCase):
    def test_no_quote_and_deposit_amount_missing_warn_without_holding(self):
        self.hub.deals["D1"] = make_deal(deposit_amount="")
        plan = self.plan()
        self.assertIn("no_quote", plan["warnings"])
        self.assertIn("deposit_amount_missing", plan["warnings"])
        self.assertEqual(plan["status"], "ready")  # warnings never hold
        # Left Nursery gets no deposit, so a missing amount is not a warning there.
        self.hub.stages["s-left"] = ("Operations", "Left Nursery")
        self.hub.deals["D1"] = make_deal(dealstage="s-left", deposit_amount="",
                                         end_date="2026-06-30")
        self.assertNotIn("deposit_amount_missing", self.plan()["warnings"])
        self.hub.deals["D1"] = make_deal()
        self.assertNotIn("deposit_amount_missing", self.plan()["warnings"])

    def test_every_contact_lists_its_link_labels_by_name_and_type_id(self):
        contacts = self.plan()["contacts"]
        self.assertEqual(contacts[0]["labels"], [
            {"typeId": 91, "label": "Bill Payer", "direction": "deal_to_contact"}])
        self.assertEqual(contacts[1]["labels"], [
            {"typeId": 3, "label": None, "direction": "deal_to_contact"}])


# --------------------------------------------------------------------------- #
# Per-contact fields: phone, address, name, emergency contact, records.
# --------------------------------------------------------------------------- #
class ContactFieldTests(DryRunTestCase):
    def set_contact(self, contact_id, **props):
        base = {"email": f"{contact_id}@example.org"}
        base.update(props)
        self.hub.contacts[contact_id] = base

    def entry(self, index=0):
        return self.plan()["contacts"][index]

    def test_phone_order_is_mobile_then_work_then_whatsapp(self):
        wa = hubspot.WHATSAPP_PROPERTY
        cases = [
            (dict(mobilephone="07700900123", phone="02070000000", **{wa: "07700900555"}),
             "mobile", "MOBILE"),
            (dict(mobilephone="  ", phone="02070000000", **{wa: "07700900555"}),
             "work", "WORK"),
            (dict(**{wa: "07700900555"}), "whatsapp", "MOBILE"),
            (dict(), "none", None),
        ]
        for props, source, famly_type in cases:
            with self.subTest(source=source):
                self.set_contact("c1", **props)
                entry = self.entry()
                self.assertEqual((entry["phoneSource"], entry["phoneType"]),
                                 (source, famly_type))

    def test_phone_missing_warns_once_per_deal_and_never_holds(self):
        plan = self.plan()  # neither default contact has any phone
        self.assertEqual(plan["warnings"].count("phone_missing"), 1)
        self.assertEqual(plan["status"], "ready")
        self.set_contact("c1", mobilephone="07700900123")
        self.set_contact("c2", phone="02070000000")
        self.assertNotIn("phone_missing", self.plan()["warnings"])

    def test_a_complete_address_needs_street_zip_and_city_only(self):
        full = dict(address="1 Mill Lane", zip="E1 1AA", city="London")
        self.set_contact("c1", **full)
        self.assertTrue(self.entry()["hasCompleteAddress"])  # state and country not needed
        for missing in ("address", "zip", "city"):
            with self.subTest(missing=missing):
                props = dict(full, state="Greater London", country="UK")
                props[missing] = "  "
                self.set_contact("c1", **props)
                self.assertFalse(self.entry()["hasCompleteAddress"])
                self.assertIn("address_incomplete", self.plan()["warnings"])
        self.set_contact("c1", **full)
        self.set_contact("c2", **full)
        self.assertNotIn("address_incomplete", self.plan()["warnings"])

    def test_name_and_email_flags_and_the_emergency_contact_rule(self):
        self.set_contact("c1", firstname="Ada", lastname="Lovelace")
        self.set_contact("c2", firstname="Ben")
        first, second = self.plan()["contacts"]
        self.assertEqual((first["hasFirstAndLastName"], second["hasFirstAndLastName"]),
                         (True, False))
        self.assertTrue(first["hasEmail"] and second["hasEmail"])
        # Emergency contact: the bill payer (c1) only.
        self.assertEqual((first["isBillPayer"], first["emergencyContact"]), (True, True))
        self.assertEqual((second["isBillPayer"], second["emergencyContact"]), (False, False))
        self.hub.contacts["c2"] = {"email": ""}
        self.assertFalse(self.entry(1)["hasEmail"])

    def test_every_contact_carries_the_relation_record_parent(self):
        for entry in self.plan()["contacts"]:
            self.assertEqual(entry["records"], [{"key": "RELATION", "value": "Parent"}])

    def test_no_phone_address_or_name_value_reaches_the_output(self):
        self.set_contact("c1", firstname="Ada", lastname="Lovelace", mobilephone="07700911111",
                         address="1 Mill Lane", zip="E1 1AA", city="London",
                         state="Greater London", country="UK")
        self.set_contact("c2", phone="02079998888", **{hubspot.WHATSAPP_PROPERTY: "07700922222"})
        blob = json.dumps(self.plan()).casefold()
        for value in ("ada", "lovelace", "07700911111", "mill lane", "e1 1aa", "london",
                      "02079998888", "07700922222", "uk"):
            if value == "uk":
                self.assertNotIn('"uk"', blob)
            else:
                self.assertNotIn(value, blob, value)


if __name__ == "__main__":
    unittest.main()
