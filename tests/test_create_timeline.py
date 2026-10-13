"""Tests for the create_timeline action: a starter's plans as a chain, one Slack
approval per plan.

Run from the project root:

    python -m unittest tests.test_create_timeline -v

NOTHING HERE TOUCHES FAMLY OR SLACK. `RestClient` is replaced by `FakeFamly`, a
small stateful stand-in that keeps the child's plans, trims the previous plan
when a plan is created with `oldPlanId` (as Famly does, confirmed by manual
test) and can be told not to, or to fail a commit. The Slack posters are
recorders, and the background work an approval hands off runs inline so the
assertions are deterministic.

The flat HubSpot payload goes through the REAL flattener against fixture
catalogues, so what a funded or non-funded plan books is genuinely checked.
"""

import json
import logging
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from actions.create_timeline import chains, engine, maintenance
from actions.create_timeline import web as create_timeline_web
from actions.plan_write import approval, preview_store, runner
from actions.read_child_plans import runner as read_child_plans
from core.rest_client import RestHTTPError
from integrations import child_lookup, slack

logging.disable(logging.CRITICAL)

CHILD = "00000000-0000-0000-0000-0000000000c1"
INSTITUTION = "HDCITY"
SCHEDULE = "00000000-0000-0000-0000-000000000003"
BILLING_PROFILE = "cccccccc-0000-0000-0000-000000000099"

FUNDED_SESSION = "11111111-0000-0000-0000-000000000001"
NON_FUNDED_SESSION = "55555555-0000-0000-0000-000000000005"
MEALS_PRODUCT = "66666666-0000-0000-0000-000000000006"
ACTIVITIES_PRODUCT = "77777777-0000-0000-0000-000000000007"


def _slots(uuid_value):
    return {"morning": uuid_value, "afternoon": uuid_value, "full_day": uuid_value}


SESSION_CATALOGUE = {
    "institutions": {
        INSTITUTION: {
            "funded": {
                "with_meals_and_activities": _slots(FUNDED_SESSION),
                "no_meals": _slots(FUNDED_SESSION),
                "no_activities": _slots(FUNDED_SESSION),
                "neither": _slots(FUNDED_SESSION),
            },
            "non_funded": _slots(NON_FUNDED_SESSION),
        }
    }
}

_BUCKET = {
    "billingProfileId": BILLING_PROFILE,
    "attendanceScheduleId": SCHEDULE,
    "weeksOfCare": 51,
    "billingId": "ANNUALIZED_V2",
    "billingTitle": "Monthly",
    "billingInvoices": "ADVANCE",
}
INSTITUTION_DEFAULTS = {
    "institutions": {
        INSTITUTION: {
            "ruleGroupId": "01RULEGROUP0000000000000000",
            "schedules": {"all_year_round": _BUCKET, "term_only": _BUCKET},
            "addonProducts": {
                "mealsProductId": MEALS_PRODUCT,
                "activitiesProductId": ACTIVITIES_PRODUCT,
                "halfMealsProductId": None,
                "halfActivitiesProductId": None,
            },
        }
    }
}


def payload(**overrides) -> dict:
    """A flat HubSpot payload for a starter, with the timeline fields."""
    body = {
        "childId": CHILD,
        "institution": INSTITUTION,
        "monday": "full day",
        "tuesday": "full day",
        "wednesday": "morning",
        "termTimeOnly": "false",
        "has_meals": "true",
        "has_activities": "true",
        "fundingMethod": "STRETCHED",
        "fundingHours": 15,
        "maxFundedMinutes": 900,
        "addon_quantity": "2",
        "note": "Starter",
        "start_date": "2026-09-01",
        "leave_date": "2027-07-31",
        # Never to be kept: names are not on the allow-list.
        "child_name": "Alex Smith",
        "parent_name": "Pat Smith",
    }
    body.update(overrides)
    return body


def with_funding(start="2026-11-01", end="2026-12-31", **overrides) -> dict:
    return payload(has_funding="true", funding_start=start, funding_end=end, **overrides)


class FakeFamly:
    """The REST client both preview/commit and read_child_plans use."""

    def __init__(self):
        self.plans: list[dict] = []
        self.posts: list[dict] = []  # {"params", "body"} for every POST
        self.gets = 0
        self.trim = True
        self.fail_commit_number: int | None = None
        self._commits = 0

    # -- reads ------------------------------------------------------------ #
    def get(self, path, params=None):
        self.gets += 1
        return {"plans": [dict(p) for p in self.plans]}

    # -- writes ----------------------------------------------------------- #
    def post(self, path, params=None, json_body=None):
        params = dict(params or {})
        self.posts.append({"params": params, "body": json_body})
        plan = json_body["plan"]
        node = {
            "id": "",
            "childId": plan["childId"],
            "from": plan["from"],
            "to": plan.get("to"),
            "planParts": [],
            "behaviors": [],
            "planStates": [
                {
                    "from": plan["from"],
                    "to": plan.get("to"),
                    "pricingGroupId": "pg-1",
                    "monthlyEstimate": 500.0,
                }
            ],
        }
        if params["preview"] == "true":
            return node

        self._commits += 1
        if self.fail_commit_number == self._commits:
            raise RestHTTPError("Famly exploded", 500, "{}")

        node["id"] = f"plan-{self._commits}"
        old_id = params.get("oldPlanId")
        if old_id and self.trim:
            for existing in self.plans:
                if existing["id"] == old_id:
                    start = date.fromisoformat(plan["from"])
                    existing["to"] = (start - timedelta(days=1)).isoformat()
        self.plans.append(
            {
                "id": node["id"],
                "childId": plan["childId"],
                "from": plan["from"],
                "to": plan.get("to"),
            }
        )
        return node

    # -- helpers ---------------------------------------------------------- #
    @property
    def previews(self):
        return [c for c in self.posts if c["params"]["preview"] == "true"]

    @property
    def commits(self):
        return [c for c in self.posts if c["params"]["preview"] == "false"]


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        (root / "sessions.json").write_text(json.dumps(SESSION_CATALOGUE), encoding="utf-8")
        (root / "defaults.json").write_text(json.dumps(INSTITUTION_DEFAULTS), encoding="utf-8")

        env = mock.patch.dict(
            "os.environ",
            {
                "PREVIEW_STORE_PATH": str(root / "store.sqlite3"),
                "PREVIEW_TTL_HOURS": "24",
                "FAMLY_ACCESS_TOKEN": "test-token",
                "COMMIT_ENABLED": "true",
                "COMMIT_ALLOWED_CHILD_IDS": CHILD,
                "SESSION_CATALOGUE_FILE": str(root / "sessions.json"),
                "INSTITUTION_DEFAULTS_FILE": str(root / "defaults.json"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.store_file = root / "store.sqlite3"

        self.famly = FakeFamly()
        for module in (runner, read_child_plans):
            patcher = mock.patch.object(module, "RestClient", lambda *a, **k: self.famly)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.previews_posted: list[tuple[str, str]] = []
        self.adjusted_posted: list[tuple[str, str]] = []
        self.notices: list[str] = []
        for name, replacement in (
            ("post_preview", lambda text, pid: self.previews_posted.append((text, pid)) or True),
            ("post_adjusted", lambda text, pid: self.adjusted_posted.append((text, pid)) or True),
            ("post_notice", lambda text: self.notices.append(text) or True),
            ("update_message", lambda url, text: True),
        ):
            patcher = mock.patch.object(slack, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Never reach the network for the child's name.
        patcher = mock.patch.object(child_lookup, "lookup_child_name", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The read-back retry never really waits; the delays are recorded.
        self.sleeps: list[float] = []
        patcher = mock.patch.object(engine, "_sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The background work an approval hands off runs inline.
        patcher = mock.patch.object(approval, "_spawn", lambda work: work())
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- driving the flow -------------------------------------------------- #
    def start(self, body=None):
        return create_timeline_web.handle(body if body is not None else payload())

    def approve(self, preview_id=None):
        return approval.handle_click(
            slack.ACTION_APPROVE, preview_id or self.last_preview_id(), "drew"
        )

    def approve_then(self, change, preview_id=None):
        """Approve, running `change()` the moment Famly has written the plan.

        The live check reads the child's plans BEFORE the write, so anything
        meant to go wrong in the read-back has to happen after it.
        """
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            if (params or {}).get("preview") == "false":
                change()
            return node

        with mock.patch.object(self.famly, "post", post):
            return self.approve(preview_id)

    def reject(self, preview_id=None):
        return approval.handle_click(
            slack.ACTION_REJECT, preview_id or self.last_preview_id(), "drew"
        )

    def last_preview_id(self):
        return self.previews_posted[-1][1]

    def last_text(self):
        return self.previews_posted[-1][0]

    def heading(self, index=-1):
        return self.previews_posted[index][0].splitlines()[0]

    def run_all(self, body=None):
        """Start, then approve every plan until the chain stops asking."""
        data, status = self.start(body)
        self.assertEqual(status, 200, data)
        seen = 0
        while len(self.previews_posted) > seen:
            seen = len(self.previews_posted)
            self.approve()
        return data

    def chain(self, data):
        return chains.get(data["chain_id"])

    def sent(self, call):
        """The plan part of a recorded POST body."""
        return call["body"]["plan"]

    def session_ids(self, call):
        return {
            b["sessionId"] for b in self.sent(call)["planParts"][0]["sessionBookings"]
        }

    def fundable_flags(self, call):
        return {
            b["fundable"] for b in self.sent(call)["planParts"][0]["sessionBookings"]
        }

    def add_surprise_plan(self):
        self.famly.plans.append(
            {"id": "surprise", "childId": CHILD, "from": "2030-01-01", "to": None}
        )

    def notices_text(self):
        return "\n".join(self.notices)


# --------------------------------------------------------------------------- #
# The timelines
# --------------------------------------------------------------------------- #
class BaseOnlyTests(ChainTestCase):
    def test_a_child_with_no_funding_gets_one_plan(self):
        data, status = self.start()

        self.assertEqual(status, 200)
        self.assertEqual(len(data["segments"]), 1)
        self.assertEqual(len(self.previews_posted), 1)
        self.assertEqual(self.heading(), "*Plan 1 of 1 (base, 2026-09-01 to 2027-07-31)*")

    def test_the_only_plan_is_a_first_plan_with_an_end_date_and_no_old_plan(self):
        self.start()
        call = self.famly.previews[0]

        self.assertNotIn("oldPlanId", call["params"])
        self.assertNotIn("replaceOldPlan", call["params"])
        self.assertEqual(self.sent(call)["from"], "2026-09-01")
        self.assertEqual(self.sent(call)["to"], "2027-07-31")

    def test_approving_it_completes_the_chain(self):
        data = self.run_all()

        self.assertEqual(len(self.famly.commits), 1)
        self.assertEqual(self.chain(data).status, chains.STATUS_COMPLETE)
        self.assertTrue(any("All 1 plans created" in n for n in self.notices))

    def test_a_non_funded_plan_books_the_add_on_products_and_no_funding(self):
        self.start()
        call = self.famly.previews[0]
        part = self.sent(call)["planParts"][0]

        self.assertEqual(self.session_ids(call), {NON_FUNDED_SESSION})
        self.assertEqual(self.fundable_flags(call), {False})
        self.assertEqual(
            {b["productId"] for b in part["productBookings"]},
            {MEALS_PRODUCT, ACTIVITIES_PRODUCT},
        )
        self.assertNotIn("publicFundingSettings", self.sent(call))


class FundedInTheMiddleTests(ChainTestCase):
    def test_three_plans_in_order_with_the_funded_heading(self):
        data, status = self.start(with_funding())

        self.assertEqual(status, 200)
        self.assertEqual([s["funded"] for s in data["segments"]], [False, True, False])
        self.assertEqual(self.heading(), "*Plan 1 of 3 (base, 2026-09-01 to 2026-10-31)*")

        self.approve()
        self.assertEqual(
            self.heading(), "*Plan 2 of 3 (funded, 2026-11-01 to 2026-12-31)*"
        )

        self.approve()
        self.assertEqual(self.heading(), "*Plan 3 of 3 (base, 2027-01-01 to 2027-07-31)*")

    def test_only_the_final_plan_is_created_with_an_end_date(self):
        self.run_all(with_funding())

        ends = [self.sent(c)["to"] for c in self.famly.commits]
        self.assertEqual(ends, [None, None, "2027-07-31"])
        starts = [self.sent(c)["from"] for c in self.famly.commits]
        self.assertEqual(starts, ["2026-09-01", "2026-11-01", "2027-01-01"])

    def test_the_funded_plan_books_funded_sessions_and_funding_and_no_add_ons(self):
        self.run_all(with_funding())
        base_first, funded, base_last = self.famly.commits

        self.assertEqual(self.session_ids(funded), {FUNDED_SESSION})
        self.assertEqual(self.fundable_flags(funded), {True})
        self.assertEqual(self.sent(funded)["planParts"][0]["productBookings"], [])
        settings = self.sent(funded)["publicFundingSettings"]
        self.assertEqual(settings["method"], "STRETCHED")
        self.assertEqual(settings["hours"], 15)

        for plan in (base_first, base_last):
            self.assertEqual(self.session_ids(plan), {NON_FUNDED_SESSION})
            self.assertNotIn("publicFundingSettings", self.sent(plan))
            self.assertEqual(
                {b["productId"] for b in self.sent(plan)["planParts"][0]["productBookings"]},
                {MEALS_PRODUCT, ACTIVITIES_PRODUCT},
            )

    def test_the_chain_completes_and_says_so(self):
        data = self.run_all(with_funding())

        self.assertEqual(self.chain(data).status, chains.STATUS_COMPLETE)
        self.assertEqual(self.chain(data).next_index, 3)
        self.assertTrue(any("All 3 plans created" in n for n in self.notices))
        self.assertEqual(len(self.previews_posted), 3)

    def test_one_approval_posts_exactly_one_more_preview(self):
        self.start(with_funding())
        self.assertEqual(len(self.previews_posted), 1)

        self.approve()
        self.assertEqual(len(self.previews_posted), 2)
        self.assertEqual(len(self.famly.commits), 1)


class FundedFromTheStartTests(ChainTestCase):
    def test_the_first_plan_is_funded_and_has_no_old_plan(self):
        data, status = self.start(with_funding("2026-09-01", "2026-12-18"))

        self.assertEqual(status, 200)
        self.assertEqual([s["funded"] for s in data["segments"]], [True, False])
        self.assertEqual(self.heading(), "*Plan 1 of 2 (funded, 2026-09-01 to 2026-12-18)*")

        call = self.famly.previews[0]
        self.assertNotIn("oldPlanId", call["params"])
        self.assertEqual(self.fundable_flags(call), {True})
        self.assertIn("publicFundingSettings", self.sent(call))


class OverlapTests(ChainTestCase):
    """Advanced overlapping the funding start: five plans."""

    def body(self):
        return payload(
            has_funding="true",
            funding_start="2026-11-01",
            funding_end="2027-03-31",
            has_advanced_fee="true",
            advanced_start="2026-10-01",
            advanced_end="2026-11-30",
            advanced_discount="40",
        )

    def test_five_plans_with_the_right_flags_and_headings(self):
        data, status = self.start(self.body())

        self.assertEqual(status, 200)
        self.assertEqual(
            [(s["from"], s["to"], s["funded"], s["advanced"]) for s in data["segments"]],
            [
                ("2026-09-01", "2026-09-30", False, False),
                ("2026-10-01", "2026-10-31", False, True),
                ("2026-11-01", "2026-11-30", True, True),
                ("2026-12-01", "2027-03-31", True, False),
                ("2027-04-01", "2027-07-31", False, False),
            ],
        )

        headings = [self.heading()]
        for _ in range(4):
            self.approve()
            headings.append(self.heading())
        self.assertEqual(
            headings,
            [
                "*Plan 1 of 5 (base, 2026-09-01 to 2026-09-30)*",
                "*Plan 2 of 5 (base, advanced, 2026-10-01 to 2026-10-31)*",
                "*Plan 3 of 5 (funded, advanced, 2026-11-01 to 2026-11-30)*",
                "*Plan 4 of 5 (funded, 2026-12-01 to 2027-03-31)*",
                "*Plan 5 of 5 (base, 2027-04-01 to 2027-07-31)*",
            ],
        )

    def test_the_advanced_fee_discount_is_in_slot_10_of_advanced_plans_only(self):
        self.run_all(self.body())

        def advanced_discounts(call):
            return [
                d
                for d in self.sent(call)["planParts"][0]["discounts"]
                if d["title"] == engine.ADVANCED_FEE_DISCOUNT_TITLE
            ]

        flags = [bool(advanced_discounts(c)) for c in self.famly.commits]
        self.assertEqual(flags, [False, True, True, False, False])

        discount = advanced_discounts(self.famly.commits[1])[0]
        self.assertEqual(discount["ordering"], 9)  # slot 10
        self.assertEqual(discount["amount"], 40)
        self.assertIs(discount["isPercent"], False)
        self.assertEqual(discount["period"], "WEEKLY")

    def test_a_fixed_40_gives_a_fixed_discount_in_every_advanced_segment(self):
        self.run_all(self.body())

        advanced_commits = [self.famly.commits[i] for i in (1, 2)]
        for call in advanced_commits:
            fixed = [
                d
                for d in self.sent(call)["planParts"][0]["discounts"]
                if d["title"] == engine.ADVANCED_FEE_DISCOUNT_TITLE
            ]
            self.assertEqual(len(fixed), 1)
            self.assertEqual(fixed[0]["amount"], 40)
            self.assertIs(fixed[0]["isPercent"], False)
            self.assertEqual(fixed[0]["period"], "WEEKLY")

    def test_the_slack_summary_shows_the_fee_in_pounds(self):
        from actions.read_child_plans.runner import Plan

        self.run_all(self.body())
        built = [
            d
            for d in self.sent(self.famly.commits[1])["planParts"][0]["discounts"]
            if d["title"] == engine.ADVANCED_FEE_DISCOUNT_TITLE
        ]

        summary = slack.build_summary(Plan(id="p", child_id=CHILD, discounts=built), [])

        self.assertIn("Advanced Fee Discount — £40.00", summary)
        self.assertNotIn("4000%", summary)

    def test_a_funded_advanced_plan_is_both(self):
        self.run_all(self.body())
        both = self.famly.commits[2]

        self.assertEqual(self.fundable_flags(both), {True})
        self.assertIn("publicFundingSettings", self.sent(both))
        self.assertEqual(
            [d["title"] for d in self.sent(both)["planParts"][0]["discounts"]],
            [engine.ADVANCED_FEE_DISCOUNT_TITLE],
        )

    def test_the_last_four_advanced_start_and_leave_dates_chain_up(self):
        self.run_all(self.body())

        self.assertEqual(
            [(p["from"], p["to"]) for p in self.famly.plans],
            [
                ("2026-09-01", "2026-09-30"),
                ("2026-10-01", "2026-10-31"),
                ("2026-11-01", "2026-11-30"),
                ("2026-12-01", "2027-03-31"),
                ("2027-04-01", "2027-07-31"),
            ],
        )


class AdvancedAtTheEndTests(ChainTestCase):
    def test_plan_2_of_4_reads_as_specified(self):
        body = with_funding(
            has_advanced_fee="true",
            advanced_start="2027-03-01",
            advanced_end="2027-07-31",
            advanced_discount="40",
        )
        data, _ = self.start(body)
        self.assertEqual(len(data["segments"]), 4)

        self.approve()
        self.assertEqual(
            self.heading(), "*Plan 2 of 4 (funded, 2026-11-01 to 2026-12-31)*"
        )


class OpenEndedLeaveTests(ChainTestCase):
    def test_with_no_leave_date_the_last_plan_has_no_end_date(self):
        data = self.run_all(with_funding(leave_date=None))

        self.assertEqual([self.sent(c)["to"] for c in self.famly.commits], [None, None, None])
        self.assertEqual(self.chain(data).status, chains.STATUS_COMPLETE)

    def test_the_last_heading_says_onwards(self):
        self.start(with_funding(leave_date=None))
        self.approve()
        self.approve()

        self.assertEqual(self.heading(), "*Plan 3 of 3 (base, 2027-01-01 onwards)*")

    def test_base_only_and_open_ended_is_one_open_plan(self):
        data, _ = self.start(payload(leave_date=""))

        self.assertEqual(len(data["segments"]), 1)
        self.assertIsNone(self.sent(self.famly.previews[0])["to"])


class OldPlanIdTests(ChainTestCase):
    def test_each_plan_names_the_one_committed_before_it_and_never_replaces(self):
        self.run_all(with_funding())

        by_plan = {}
        for call in self.famly.commits:
            by_plan[self.sent(call)["from"]] = call["params"]

        self.assertNotIn("oldPlanId", by_plan["2026-09-01"])
        self.assertEqual(by_plan["2026-11-01"]["oldPlanId"], "plan-1")
        self.assertEqual(by_plan["2027-01-01"]["oldPlanId"], "plan-2")
        for params in by_plan.values():
            if "oldPlanId" in params:
                self.assertEqual(params["replaceOldPlan"], "false")

    def test_the_old_plan_is_on_the_posted_preview_too(self):
        self.start(with_funding())
        self.approve()

        second_preview = [
            c
            for c in self.famly.previews
            if self.sent(c)["from"] == "2026-11-01" and c["params"].get("oldPlanId")
        ]
        self.assertTrue(second_preview)
        self.assertEqual(second_preview[0]["params"]["oldPlanId"], "plan-1")

    def test_the_stored_preview_carries_the_old_plan_for_the_commit(self):
        self.start(with_funding())
        self.approve()

        stored = preview_store.get(self.last_preview_id())
        self.assertEqual(stored.old_plan_id, "plan-1")
        self.assertFalse(stored.replace_old_plan)

    def test_the_first_plan_is_stored_with_no_old_plan(self):
        self.start(with_funding())

        stored = preview_store.get(self.last_preview_id())
        self.assertIsNone(stored.old_plan_id)

    def test_every_stored_preview_knows_its_chain_and_position(self):
        data, _ = self.start(with_funding())
        first = preview_store.get(self.last_preview_id())
        self.approve()
        second = preview_store.get(self.last_preview_id())

        self.assertEqual((first.chain_id, first.chain_index), (data["chain_id"], 0))
        self.assertEqual((second.chain_id, second.chain_index), (data["chain_id"], 1))


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
class ExistingPlansTests(ChainTestCase):
    def test_a_child_with_a_plan_is_refused_with_a_notice_and_nothing_created(self):
        self.famly.plans.append(
            {"id": "old-plan", "childId": CHILD, "from": "2025-09-01", "to": None}
        )

        data, status = self.start(with_funding())

        self.assertEqual(status, 409)
        self.assertIsNone(data["chain_id"])
        self.assertEqual(self.famly.posts, [])
        self.assertEqual(self.previews_posted, [])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("already has", self.notices[0])
        self.assertIn("old-plan", self.notices[0])
        self.assertIsNone(chains.find_active(CHILD))

    def test_even_one_plan_with_an_end_date_is_refused(self):
        self.famly.plans.append(
            {"id": "ended", "childId": CHILD, "from": "2024-09-01", "to": "2025-07-31"}
        )

        _, status = self.start()

        self.assertEqual(status, 409)
        self.assertEqual(self.previews_posted, [])

    def test_a_failed_plans_read_is_not_taken_as_no_plans(self):
        def boom(*args, **kwargs):
            raise RestHTTPError("Famly down", 503, "")

        with mock.patch.object(self.famly, "get", boom):
            with self.assertRaises(RestHTTPError):
                self.start()

        self.assertEqual(self.famly.posts, [])
        self.assertIsNone(chains.find_active(CHILD))


class DuplicateChainTests(ChainTestCase):
    def test_a_second_request_for_a_child_with_an_active_chain_creates_nothing(self):
        first, _ = self.start(with_funding())
        previews_before = len(self.previews_posted)
        posts_before = len(self.famly.posts)
        reads_before = self.famly.gets

        data, status = self.start(with_funding())

        self.assertEqual(status, 409)
        self.assertIsNone(data["chain_id"])
        self.assertEqual(len(self.previews_posted), previews_before)
        self.assertEqual(len(self.famly.posts), posts_before)
        self.assertEqual(self.famly.gets, reads_before)  # refused before any Famly call
        self.assertIn(first["chain_id"], self.notices[-1])
        self.assertIn("still active", self.notices[-1])

    def test_only_one_chain_exists(self):
        self.start(with_funding())
        self.start(with_funding())

        with preview_store.open_store() as connection:
            count = connection.execute("SELECT COUNT(*) FROM chains").fetchone()[0]
        self.assertEqual(count, 1)

    def test_the_database_itself_refuses_a_second_active_chain(self):
        segments = [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}]

        self.assertTrue(chains.create("a", CHILD, {}, segments))
        self.assertFalse(chains.create("b", CHILD, {}, segments))
        self.assertTrue(chains.create("c", "another-child", {}, segments))

    def test_a_stopped_chain_no_longer_blocks_the_database(self):
        segments = [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}]
        chains.create("a", CHILD, {}, segments)
        chains.finish("a", chains.STATUS_STOPPED)

        self.assertTrue(chains.create("b", CHILD, {}, segments))


class InvalidInputTests(ChainTestCase):
    def assert_refused_without_famly(self, body, fragment):
        data, status = self.start(body)

        self.assertEqual(status, 422)
        self.assertTrue(any(fragment in e for e in data["errors"]), data["errors"])
        self.assertEqual(self.famly.posts, [])
        self.assertEqual(self.famly.gets, 0)
        self.assertEqual(self.previews_posted, [])
        self.assertIsNone(chains.find_active(CHILD))

    def test_missing_start_date(self):
        self.assert_refused_without_famly(payload(start_date=""), "start_date")

    def test_leave_before_start(self):
        self.assert_refused_without_famly(payload(leave_date="2026-08-31"), "leave")

    def test_funding_flag_without_dates_is_not_silently_no_funding(self):
        self.assert_refused_without_famly(
            payload(has_funding="true", funding_start="2026-11-01"), "funding_end"
        )

    def test_a_period_ending_before_it_starts(self):
        self.assert_refused_without_famly(
            with_funding("2026-12-31", "2026-11-01"), "before"
        )

    def test_advanced_fee_without_a_discount(self):
        self.assert_refused_without_famly(
            payload(has_advanced_fee="true", advanced_start="2026-10-01", advanced_end="2026-10-31"),
            "advanced_discount",
        )

    def test_an_out_of_range_advanced_discount_is_a_hard_error_not_a_dropped_discount(self):
        self.assert_refused_without_famly(
            payload(
                has_advanced_fee="true",
                advanced_start="2026-10-01",
                advanced_end="2026-10-31",
                advanced_discount="6000",
            ),
            "Advanced Fee Discount invalid",
        )

    def test_a_fraction_is_rejected_as_a_probable_percentage(self):
        for amount in ("0.05", "0.5", "0.99"):
            with self.subTest(amount=amount):
                self.assert_refused_without_famly(
                    payload(
                        has_advanced_fee="true",
                        advanced_start="2026-10-01",
                        advanced_end="2026-10-31",
                        advanced_discount=amount,
                    ),
                    "looks like a fraction, expected £ per week",
                )

    def test_the_limits_of_the_fixed_range(self):
        for amount, accepted in (("1", True), ("5000", True), ("5000.01", False), ("0", False), ("-40", False)):
            with self.subTest(amount=amount):
                data, status = self.start(
                    payload(
                        has_advanced_fee="true",
                        advanced_start="2026-10-01",
                        advanced_end="2026-10-31",
                        advanced_discount=amount,
                    )
                )
                self.assertEqual(status == 200, accepted, data["errors"])
                if data["chain_id"]:
                    chains.finish(data["chain_id"], chains.STATUS_CANCELLED)

    def test_slot_10_already_in_use(self):
        self.assert_refused_without_famly(
            payload(
                has_advanced_fee="true",
                advanced_start="2026-10-01",
                advanced_end="2026-10-31",
                advanced_discount="40",
                discount_10_name="Sibling",
                discount_10_amount="0.05",
            ),
            "slot 10",
        )

    def test_a_bad_later_plan_is_found_before_the_first_is_posted(self):
        # No funded session in the catalogue for this institution's variants.
        with mock.patch.dict(
            "os.environ", {"SESSION_CATALOGUE_FILE": str(Path(self._tmp.name) / "none.json")}
        ):
            data, status = self.start(with_funding())

        self.assertEqual(status, 422)
        self.assertTrue(any(e.startswith("Plan ") for e in data["errors"]), data["errors"])
        self.assertEqual(self.famly.posts, [])

    def test_a_dropped_period_is_reported_and_the_rest_still_runs(self):
        data, status = self.start(with_funding("2020-01-01", "2020-12-31"))

        self.assertEqual(status, 200)
        self.assertEqual(len(data["segments"]), 1)
        self.assertTrue(any("wholly outside" in w for w in data["warnings"]))
        self.assertTrue(any("wholly outside" in n for n in self.notices))


# --------------------------------------------------------------------------- #
# Stopping
# --------------------------------------------------------------------------- #
class RejectStopsTheChainTests(ChainTestCase):
    def test_rejecting_plan_2_stops_the_chain_and_says_what_exists(self):
        data, _ = self.start(with_funding())
        self.approve()  # plan 1
        previews_before = len(self.previews_posted)

        text = self.reject()

        self.assertEqual(text, approval.MSG_REJECTED)
        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertEqual(len(self.previews_posted), previews_before)  # plan 3 never posted
        self.assertEqual(len(self.famly.commits), 1)

        notice = self.notices[-1]
        self.assertIn("Timeline stopped", notice)
        self.assertIn("rejected", notice)
        self.assertIn("plan `plan-1`", notice)  # exists
        exists, _, missing = notice.partition("Plans NOT created")
        exists = exists.partition("Plans that exist")[2]
        self.assertIn("Plan 1 of 3", exists)
        self.assertIn("Plan 2 of 3", missing)
        self.assertIn("Plan 3 of 3", missing)
        self.assertNotIn("Plan 2 of 3", exists)

    def test_the_stop_says_the_last_committed_plan_is_open_ended(self):
        self.start(with_funding())
        self.approve()
        self.reject()

        notice = self.notices[-1]
        self.assertIn("open-ended", notice)
        self.assertIn("end date or its successor", notice)
        self.assertIn("`plan-1`", notice.split("open-ended")[0].splitlines()[-1])

    def test_rejecting_plan_1_stops_with_nothing_created_and_no_open_ended_claim(self):
        data, _ = self.start(with_funding())

        self.reject()

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertEqual(self.famly.commits, [])
        notice = self.notices[-1]
        self.assertIn("none — nothing was created", notice)
        self.assertNotIn("open-ended", notice)

    def test_a_stopped_chain_cannot_be_started_again_over_existing_plans(self):
        self.start(with_funding())
        self.approve()
        self.reject()

        _, status = self.start(with_funding())

        self.assertEqual(status, 409)  # the committed plan 1 is an existing plan

    def test_rejecting_an_ordinary_preview_touches_no_chain(self):
        preview_store.save("plain", {"plan": {"childId": CHILD}}, CHILD, 3)

        self.assertEqual(approval.handle_click(slack.ACTION_REJECT, "plain", "drew"), approval.MSG_REJECTED)
        self.assertEqual(self.notices, [])

    def test_rejecting_after_the_plan_was_already_committed_does_not_stop_the_chain(self):
        data, _ = self.start(with_funding())
        first_preview = self.last_preview_id()
        self.approve()

        self.reject(first_preview)  # a stale click on plan 1's old message

        self.assertEqual(self.chain(data).status, chains.STATUS_ACTIVE)


class CommitFailureStopsTheChainTests(ChainTestCase):
    def setUp(self):
        super().setUp()
        self.famly.fail_commit_number = 2

    def test_a_failed_commit_stops_the_chain_and_never_retries(self):
        data, _ = self.start(with_funding())
        self.approve()
        failed_preview = self.last_preview_id()
        posts_before = len(self.famly.posts)

        text = self.approve()

        self.assertIn("Commit failed", text)
        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertEqual(len(self.famly.commits), 2)  # attempted once, not again
        self.assertEqual(len(self.famly.posts), posts_before + 2)  # its preview + its write
        self.assertEqual(len(self.previews_posted), 2)  # plan 3 never posted

        # The same button cannot be clicked into a second write.
        again = self.approve(failed_preview)
        self.assertEqual(again, approval.MSG_CHAIN_STOPPED)
        self.assertEqual(len(self.famly.posts), posts_before + 2)
        self.assertEqual(preview_store.get(failed_preview).status, preview_store.STATUS_FAILED)

    def test_slack_is_told_what_exists_and_what_does_not(self):
        self.start(with_funding())
        self.approve()
        self.approve()

        notice = self.notices[-1]
        exists, _, missing = notice.partition("Plans NOT created")
        self.assertIn("could not be committed", notice)
        self.assertIn("plan `plan-1`", exists)
        self.assertIn("Plan 2 of 3", missing)
        self.assertIn("check Famly", missing)
        self.assertIn("Plan 3 of 3", missing)
        self.assertIn("open-ended", notice)
        self.assertIn("end date or its successor", notice)

    def test_a_failure_on_an_ordinary_preview_still_goes_back_to_pending(self):
        # Unchanged behaviour: only chain plans are never released.
        plain_body = {"plan": {"childId": CHILD, "from": "2026-09-01", "planParts": []}}
        preview_store.save("plain", plain_body, CHILD, 3)
        self.famly.fail_commit_number = 1

        approval.handle_click(slack.ACTION_APPROVE, "plain", "drew")

        self.assertEqual(preview_store.get("plain").status, preview_store.STATUS_PENDING)

    def test_a_refused_commit_before_the_claim_leaves_the_chain_alone(self):
        data, _ = self.start(with_funding())
        with mock.patch.dict("os.environ", {"COMMIT_ENABLED": "false"}):
            text = self.approve()

        self.assertEqual(text, approval.MSG_COMMIT_DISABLED)
        self.assertEqual(self.chain(data).status, chains.STATUS_ACTIVE)
        self.assertEqual(self.chain(data).next_index, 0)


class VerificationTests(ChainTestCase):
    def test_a_previous_plan_that_was_not_trimmed_stops_the_chain_with_both_plans(self):
        self.famly.trim = False
        data, _ = self.start(with_funding())
        self.approve()  # plan 1: nothing to verify against
        previews_before = len(self.previews_posted)
        self.approve()  # plan 2: plan 1 should now end 2026-10-31, but does not

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertEqual(len(self.previews_posted), previews_before)  # plan 3 not posted

        notice = self.notices[-1]
        self.assertIn("did not end the previous plan", notice)
        self.assertIn("expected to = 2026-10-31", notice)
        self.assertIn("`plan-1`: from 2026-09-01 to open-ended", notice)
        self.assertIn("`plan-2`: from 2026-11-01 to open-ended", notice)
        self.assertIn("open-ended", notice.split("Reason")[1])
        self.assertIn("end date or its successor", notice)

    def test_the_stop_after_a_bad_trim_counts_the_new_plan_as_existing(self):
        self.famly.trim = False
        data, _ = self.start(with_funding())
        self.approve()
        self.approve()

        chain = self.chain(data)
        self.assertEqual(chain.next_index, 2)
        self.assertEqual([s["plan_id"] for s in chain.segments], ["plan-1", "plan-2", None])

    def test_a_trim_to_the_wrong_day_is_caught(self):
        data, _ = self.start(with_funding())
        self.approve()

        original_post = self.famly.post

        def post_trimming_one_day_late(path, params=None, json_body=None):
            node = original_post(path, params, json_body)
            if (params or {}).get("preview") == "false":
                self.famly.plans[0]["to"] = "2026-11-01"  # should be 2026-10-31
            return node

        with mock.patch.object(self.famly, "post", post_trimming_one_day_late):
            self.approve()

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertIn("expected to = 2026-10-31", self.notices[-1])

    def test_a_committed_plan_missing_from_the_read_back_stops_the_chain(self):
        data, _ = self.start(with_funding())

        with mock.patch.object(self.famly, "get", lambda *a, **k: {"plans": []}):
            self.approve()

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertIn("could not be verified", self.notices[-1])
        self.assertEqual(len(self.previews_posted), 1)
        self.assertEqual(self.sleeps, [5, 5, 5])  # three retries, 5s apart

    def test_a_failed_read_back_stops_the_chain_instead_of_raising(self):
        data, _ = self.start(with_funding())

        def read_fails():
            self.famly.get = boom

        self.approve_then(read_fails)  # must not raise

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertIn("RestHTTPError", self.notices[-1])
        self.assertEqual(len(self.previews_posted), 1)

    def test_a_later_starting_plan_stops_the_chain(self):
        data, _ = self.start(with_funding())

        self.approve_then(self.add_surprise_plan)

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertIn("latest-starting", self.notices[-1])
        self.assertEqual(len(self.previews_posted), 1)

    def test_the_last_plan_is_verified_too(self):
        self.famly.trim = False
        data = self.run_all(with_funding())

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertFalse(any("All 3 plans created" in n for n in self.notices))


class StopMessageTests(ChainTestCase):
    def test_a_stop_after_the_last_plan_does_not_claim_an_open_ended_plan(self):
        chain = chains.Chain(
            chain_id="c",
            child_id=CHILD,
            segments=[
                {"from": "2026-09-01", "to": "2026-10-31", "funded": False, "advanced": False, "plan_id": "p1"},
                {"from": "2026-11-01", "to": "2027-07-31", "funded": False, "advanced": False, "plan_id": "p2"},
            ],
            next_index=2,
        )

        message = engine.stop_message(chain, "something went wrong")

        self.assertNotIn("open-ended", message)
        self.assertIn("`p1`", message)
        self.assertIn("`p2`", message)
        self.assertNotIn("Plans NOT created", message)


# --------------------------------------------------------------------------- #
# Adjust & Approve
# --------------------------------------------------------------------------- #
class AdjustKeepsTheChainTests(ChainTestCase):
    def adjust(self, preview_id, value="0.50"):
        return approval.handle_adjust_submission(
            preview_id, value, "drew", spawn=lambda work: work()
        )

    def test_the_adjusted_preview_carries_the_chain_id_index_and_old_plan(self):
        data, _ = self.start(with_funding())
        self.approve()  # plan 1
        original = self.last_preview_id()

        self.assertIsNone(self.adjust(original))

        adjusted_id = self.adjusted_posted[-1][1]
        adjusted = preview_store.get(adjusted_id)
        self.assertEqual((adjusted.chain_id, adjusted.chain_index), (data["chain_id"], 1))
        self.assertEqual(adjusted.old_plan_id, "plan-1")
        self.assertFalse(adjusted.replace_old_plan)
        self.assertEqual(preview_store.get(original).status, preview_store.STATUS_SUPERSEDED)

    def test_the_adjusted_message_says_which_plan_of_the_chain_it_is(self):
        self.start(with_funding())
        self.approve()
        self.adjust(self.last_preview_id())

        text = self.adjusted_posted[-1][0]
        self.assertIn("• Plan 2 of 3 (funded, 2026-11-01 to 2026-12-31)", text)

    def test_the_adjusted_re_price_is_sent_with_the_old_plan_id(self):
        self.start(with_funding())
        self.approve()
        self.adjust(self.last_preview_id())

        last_preview = self.famly.previews[-1]
        self.assertEqual(last_preview["params"]["oldPlanId"], "plan-1")
        self.assertEqual(last_preview["params"]["replaceOldPlan"], "false")

    def test_committing_the_adjusted_plan_advances_the_chain(self):
        data, _ = self.start(with_funding())
        self.approve()  # plan 1 -> plan 2 posted
        self.adjust(self.last_preview_id())

        self.approve(self.adjusted_posted[-1][1])  # confirm commit of the adjusted plan 2

        chain = self.chain(data)
        self.assertEqual(chain.next_index, 2)
        self.assertEqual(chain.segments[1]["plan_id"], "plan-2")
        self.assertEqual(self.heading(), "*Plan 3 of 3 (base, 2027-01-01 to 2027-07-31)*")
        committed = self.famly.commits[1]
        self.assertEqual(committed["params"]["oldPlanId"], "plan-1")
        adjustments = self.sent(committed)["planParts"][0]["totalAdjustments"]
        self.assertEqual(adjustments[0]["adjustment"], 0.5)

    def test_the_superseded_original_cannot_also_advance_the_chain(self):
        data, _ = self.start(with_funding())
        self.approve()
        original = self.last_preview_id()
        self.adjust(original)

        self.assertEqual(self.approve(original), approval.MSG_SUPERSEDED)
        self.assertEqual(self.chain(data).next_index, 1)

    def test_rejecting_the_adjusted_plan_stops_the_chain(self):
        data, _ = self.start(with_funding())
        self.approve()
        self.adjust(self.last_preview_id())

        self.reject(self.adjusted_posted[-1][1])

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertIn("Plan 2 of 3", self.notices[-1])

    def test_a_failed_adjusted_commit_stops_the_chain(self):
        data, _ = self.start(with_funding())
        self.approve()
        self.adjust(self.last_preview_id())
        self.famly.fail_commit_number = 2

        self.approve(self.adjusted_posted[-1][1])

        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)


# --------------------------------------------------------------------------- #
# What is stored
# --------------------------------------------------------------------------- #
class StoredChainTests(ChainTestCase):
    def test_the_chain_row_has_the_documented_shape(self):
        data, _ = self.start(with_funding())
        chain = self.chain(data)

        self.assertEqual(chain.child_id, CHILD)
        self.assertEqual(chain.next_index, 0)
        self.assertEqual(chain.status, chains.STATUS_ACTIVE)
        self.assertTrue(chain.created_at)
        self.assertEqual(
            [(s["from"], s["to"], s["funded"], s["advanced"]) for s in chain.segments],
            [
                ("2026-09-01", "2026-10-31", False, False),
                ("2026-11-01", "2026-12-31", True, False),
                ("2027-01-01", "2027-07-31", False, False),
            ],
        )
        self.assertTrue(all(s["plan_id"] is None for s in chain.segments))

    def test_next_index_follows_the_commits(self):
        data, _ = self.start(with_funding())
        self.approve()
        self.assertEqual(self.chain(data).next_index, 1)
        self.approve()
        self.assertEqual(self.chain(data).next_index, 2)

    def test_the_payload_is_the_allow_listed_original_with_no_names(self):
        data, _ = self.start(with_funding())
        stored = self.chain(data).payload

        self.assertNotIn("child_name", stored)
        self.assertNotIn("parent_name", stored)
        self.assertEqual(stored["childId"], CHILD)
        self.assertEqual(stored["start_date"], "2026-09-01")
        self.assertEqual(stored["fundingMethod"], "STRETCHED")
        self.assertTrue(set(stored) <= engine.ALLOWED_FIELDS)

    def test_no_name_reaches_the_database_file(self):
        self.run_all(with_funding())

        with preview_store.open_store() as connection:
            rows = [tuple(r) for r in connection.execute("SELECT * FROM chains")]
            rows += [tuple(r) for r in connection.execute("SELECT * FROM previews")]
        dumped = json.dumps(rows, default=str)
        self.assertNotIn("Alex", dumped)
        self.assertNotIn("Smith", dumped)

    def test_the_original_payload_is_not_mutated(self):
        body = with_funding()
        before = json.dumps(body, sort_keys=True)

        self.start(body)

        self.assertEqual(json.dumps(body, sort_keys=True), before)


class MigrationTests(unittest.TestCase):
    def test_an_older_store_gains_the_chain_columns_and_table_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE previews (preview_id TEXT PRIMARY KEY, plan_body TEXT NOT NULL, "
                "child_id TEXT, version INTEGER, created_at TEXT NOT NULL, status TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO previews VALUES ('old', '{}', 'c', 3, '2026-01-01T00:00:00+00:00', 'pending')"
            )
            connection.commit()
            connection.close()

            with mock.patch.dict("os.environ", {"PREVIEW_STORE_PATH": str(path)}):
                preview_store.save("new", {"plan": {}}, "c", 3, chain_id="ch", chain_index=2)
                stored = preview_store.get("new")
                kept = preview_store.get("old")
                created = chains.create(
                    "ch", "c", {}, [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}]
                )

            self.assertEqual((stored.chain_id, stored.chain_index), ("ch", 2))
            self.assertIsNone(kept.chain_id)  # the pre-existing row survives untouched
            self.assertIsNone(kept.chain_index)
            self.assertTrue(created)


# --------------------------------------------------------------------------- #
# The per-segment payloads
# --------------------------------------------------------------------------- #
class SegmentPayloadTests(unittest.TestCase):
    BASE = engine.clean_payload(
        payload(
            has_funding="true",
            funding_start="2026-11-01",
            funding_end="2026-12-31",
            has_advanced_fee="true",
            advanced_start="2026-11-01",
            advanced_end="2026-12-31",
            advanced_discount="40",
            discount_1_name="Sibling",
            discount_1_amount="0.05",
        )
    )

    def segment(self, funded, advanced, start="2026-11-01", end="2026-12-31"):
        return {"from": start, "to": end, "funded": funded, "advanced": advanced}

    def test_segment_dates_replace_start_and_leave(self):
        middle = engine.segment_payload(self.BASE, self.segment(False, False), is_last=False)
        last = engine.segment_payload(self.BASE, self.segment(False, False, end="2027-07-31"), is_last=True)

        self.assertEqual((middle["from"], middle["to"]), ("2026-11-01", None))
        self.assertEqual((last["from"], last["to"]), ("2026-11-01", "2027-07-31"))

    def test_the_timeline_fields_are_not_passed_on(self):
        built = engine.segment_payload(self.BASE, self.segment(True, False), is_last=False)

        for name in engine.TIMELINE_FIELDS:
            self.assertNotIn(name, built)

    def test_a_funded_segment_keeps_funding_and_drops_add_ons(self):
        built = engine.segment_payload(self.BASE, self.segment(True, False), is_last=False)

        self.assertEqual(built["funded"], "true")
        self.assertEqual(built["fundingMethod"], "STRETCHED")
        self.assertEqual(built["fundingHours"], 15)
        self.assertNotIn("addon_quantity", built)

    def test_a_non_funded_segment_has_no_funding_settings_and_keeps_add_ons(self):
        built = engine.segment_payload(self.BASE, self.segment(False, False), is_last=False)

        self.assertEqual(built["funded"], "false")
        for name in ("fundingMethod", "fundingHours", "maxFundedMinutes"):
            self.assertNotIn(name, built)
        self.assertEqual(built["addon_quantity"], "2")

    def test_an_advanced_segment_adds_the_discount_in_slot_10_beside_the_others(self):
        built = engine.segment_payload(self.BASE, self.segment(False, True), is_last=False)

        self.assertEqual(built["discount_10_name"], "Advanced Fee Discount")
        self.assertEqual(built["discount_10_amount"], "40")
        self.assertEqual(built["discount_10_flag"], "fixed")
        self.assertEqual(built["discount_1_name"], "Sibling")

    def test_a_segment_that_is_not_advanced_has_no_slot_10(self):
        built = engine.segment_payload(self.BASE, self.segment(False, False), is_last=False)

        self.assertNotIn("discount_10_name", built)

    def test_the_base_is_never_mutated(self):
        before = json.dumps(self.BASE, sort_keys=True)
        engine.segment_payload(self.BASE, self.segment(False, True), is_last=True)
        self.assertEqual(json.dumps(self.BASE, sort_keys=True), before)

    def test_slot_10_in_use_is_an_error_not_overwritten(self):
        base = {**self.BASE, "discount_10_name": "Staff", "discount_10_amount": "0.5"}

        with self.assertRaises(engine.PayloadError):
            engine.segment_payload(base, self.segment(False, True), is_last=False)


class LabelTests(unittest.TestCase):
    SEGMENTS = [
        {"from": "2026-09-01", "to": "2026-10-31", "funded": False, "advanced": False},
        {"from": "2026-11-01", "to": "2026-12-31", "funded": True, "advanced": False},
        {"from": "2027-01-01", "to": None, "funded": False, "advanced": True},
    ]

    def test_the_heading_reads_as_specified(self):
        self.assertEqual(
            engine.heading_text(self.SEGMENTS, 1),
            "Plan 2 of 3 (funded, 2026-11-01 to 2026-12-31)",
        )

    def test_an_open_ended_segment_says_onwards(self):
        self.assertEqual(
            engine.heading_text(self.SEGMENTS, 2),
            "Plan 3 of 3 (base, advanced, 2027-01-01 onwards)",
        )


# --------------------------------------------------------------------------- #
# Hardening: read-back retry, expiry, commands, restarts
# --------------------------------------------------------------------------- #
def boom(*args, **kwargs):
    raise RestHTTPError("Famly down", 503, "")


class ReadBackRetryTests(ChainTestCase):
    def hide_new_plan_for(self, reads):
        """Make `reads` plan reads AFTER the live check's come back empty.

        The live check just before the write is read number 1 and must see the
        child's real plans; the read-back is read 2 onwards.
        """
        real_get = self.famly.get
        counter = {"n": 0}

        def get(path, params=None):
            counter["n"] += 1
            if 1 < counter["n"] <= reads + 1:
                return {"plans": []}
            return real_get(path, params)

        return mock.patch.object(self.famly, "get", get), counter

    def test_a_plan_that_shows_up_on_the_last_retry_lets_the_chain_carry_on(self):
        data, _ = self.start(with_funding())
        hide, counter = self.hide_new_plan_for(3)
        with hide:
            self.approve()

        self.assertEqual(counter["n"], 5)  # live check, the read, then 3 retries
        self.assertEqual(self.sleeps, [5, 5, 5])
        self.assertEqual(self.chain(data).status, chains.STATUS_ACTIVE)
        self.assertEqual(len(self.previews_posted), 2)  # plan 2 was posted

    def test_a_plan_that_shows_up_on_the_first_retry_waits_once(self):
        self.start(with_funding())
        hide, _ = self.hide_new_plan_for(1)
        with hide:
            self.approve()

        self.assertEqual(self.sleeps, [5])
        self.assertEqual(len(self.previews_posted), 2)

    def test_never_showing_up_stops_after_exactly_three_retries(self):
        data, _ = self.start(with_funding())
        hide, counter = self.hide_new_plan_for(99)
        with hide:
            self.approve()

        self.assertEqual(counter["n"], 5)
        self.assertEqual(self.sleeps, [5, 5, 5])
        self.assertEqual(self.chain(data).status, chains.STATUS_STOPPED)
        self.assertEqual(len(self.previews_posted), 1)

    def test_a_plan_found_first_time_never_sleeps(self):
        self.start(with_funding())
        self.approve()

        self.assertEqual(self.sleeps, [])

    def test_a_bad_trim_stops_immediately_without_retrying(self):
        self.famly.trim = False
        self.start(with_funding())
        self.approve()
        self.approve()

        self.assertEqual(self.sleeps, [])
        self.assertIn("did not end the previous plan", self.notices[-1])

    def test_a_failed_read_is_not_retried(self):
        self.start(with_funding())

        def read_fails():
            self.famly.get = boom

        self.approve_then(read_fails)

        self.assertEqual(self.sleeps, [])

    def test_a_later_starting_plan_is_not_retried(self):
        self.start(with_funding())
        self.approve_then(self.add_surprise_plan)

        self.assertEqual(self.sleeps, [])


class SweepTestCase(ChainTestCase):
    def age(self, preview_id, hours=25):
        """Make a stored preview older than its TTL."""
        old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with preview_store.open_store() as connection:
            connection.execute(
                "UPDATE previews SET created_at = ? WHERE preview_id = ?", (old, preview_id)
            )

    def in_minutes(self, minutes):
        return datetime.now(timezone.utc) + timedelta(minutes=minutes)

    def commit_without_continuing(self):
        """Approve the pending plan but lose the hand-off, as a restart would."""
        with mock.patch.object(approval, "_spawn", lambda work: None):
            self.approve()


class ExpiryTests(SweepTestCase):
    def test_an_expired_pending_plan_expires_the_chain_and_tells_slack(self):
        data, _ = self.start(with_funding())
        self.approve()  # plan 1 committed, plan 2 pending
        self.age(self.last_preview_id())

        expired = maintenance.expire_stale_chains()

        self.assertEqual(expired, [data["chain_id"]])
        self.assertEqual(self.chain(data).status, chains.STATUS_EXPIRED)
        notice = self.notices[-1]
        self.assertIn("Timeline expired", notice)
        exists, _, missing = notice.partition("Plans NOT created")
        self.assertIn("plan `plan-1`", exists)
        self.assertIn("Plan 2 of 3", missing)
        self.assertIn("Plan 3 of 3", missing)
        self.assertIn("open-ended", notice)

    def test_a_fresh_pending_plan_is_left_alone(self):
        data, _ = self.start(with_funding())

        self.assertEqual(maintenance.expire_stale_chains(), [])
        self.assertEqual(self.chain(data).status, chains.STATUS_ACTIVE)
        self.assertEqual(self.notices, [])

    def test_expiry_is_announced_once(self):
        self.start(with_funding())
        self.age(self.last_preview_id())

        maintenance.expire_stale_chains()
        maintenance.expire_stale_chains()

        self.assertEqual(len(self.notices), 1)

    def test_create_timeline_frees_a_child_whose_first_plan_expired(self):
        old, _ = self.start(with_funding())
        self.age(self.last_preview_id())

        data, status = self.start(with_funding())

        self.assertEqual(status, 200)
        self.assertNotEqual(data["chain_id"], old["chain_id"])
        self.assertEqual(chains.get(old["chain_id"]).status, chains.STATUS_EXPIRED)
        self.assertEqual(chains.find_active(CHILD).chain_id, data["chain_id"])
        self.assertTrue(any("Timeline expired" in n for n in self.notices))

    def test_create_timeline_expires_any_childs_chain_not_only_its_own(self):
        other = "00000000-0000-0000-0000-0000000000c2"
        chains.create(
            "other-chain",
            other,
            {},
            [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}],
        )
        preview_store.save("p", {"plan": {}}, other, 3, chain_id="other-chain", chain_index=0)
        self.age("p")

        self.start()

        self.assertEqual(chains.get("other-chain").status, chains.STATUS_EXPIRED)

    def test_an_expired_chain_with_a_committed_plan_still_refuses_over_existing_plans(self):
        data, _ = self.start(with_funding())
        self.approve()
        self.age(self.last_preview_id())

        _, status = self.start(with_funding())

        self.assertEqual(status, 409)  # plan 1 exists; the chain itself is freed
        self.assertEqual(self.chain(data).status, chains.STATUS_EXPIRED)

    def test_a_fresh_adjusted_copy_keeps_the_chain_alive(self):
        data, _ = self.start(with_funding())
        original = self.last_preview_id()
        approval.handle_adjust_submission(original, "0.50", "drew", spawn=lambda w: w())
        self.age(original)  # the superseded original is old; its copy is not

        self.assertEqual(maintenance.expire_stale_chains(), [])
        self.assertEqual(self.chain(data).status, chains.STATUS_ACTIVE)


class SweepTests(SweepTestCase):
    def test_sweep_expires_and_reports(self):
        data, _ = self.start(with_funding())
        self.age(self.last_preview_id())

        result = maintenance.sweep()

        self.assertEqual(result, {"expired": [data["chain_id"]], "stuck_notified": []})

    def test_a_chain_stuck_after_a_recorded_commit_gets_one_notice(self):
        data, _ = self.start(with_funding())
        self.commit_without_continuing()
        self.assertEqual(len(self.previews_posted), 1)  # plan 2 never posted

        first = maintenance.notify_stuck_chains(self.in_minutes(11))
        second = maintenance.notify_stuck_chains(self.in_minutes(30))

        self.assertEqual(first, [data["chain_id"]])
        self.assertEqual(second, [])
        stuck = [n for n in self.notices if "Timeline stuck" in n]
        self.assertEqual(len(stuck), 1)
        self.assertIn(CHILD, stuck[0])
        self.assertIn(f"chain-resume {CHILD}", stuck[0])
        self.assertIn(f"chain-cancel {CHILD}", stuck[0])
        self.assertIn("Plan 2 of 3", stuck[0])

    def test_it_waits_the_full_ten_minutes(self):
        self.start(with_funding())
        self.commit_without_continuing()

        self.assertEqual(maintenance.notify_stuck_chains(self.in_minutes(9)), [])
        self.assertEqual(len(maintenance.notify_stuck_chains(self.in_minutes(10.5))), 1)

    def test_a_chain_with_a_preview_waiting_on_an_approver_is_not_stuck(self):
        self.start(with_funding())
        self.approve()  # plan 2 is posted and pending

        self.assertEqual(maintenance.notify_stuck_chains(self.in_minutes(60)), [])

    def test_a_chain_with_nothing_committed_is_not_stuck(self):
        self.start(with_funding())

        self.assertEqual(maintenance.notify_stuck_chains(self.in_minutes(60)), [])

    def test_all_plans_committed_but_never_completed_is_stuck(self):
        data, _ = self.start(payload())  # one plan
        self.commit_without_continuing()

        self.assertEqual(
            maintenance.notify_stuck_chains(self.in_minutes(11)), [data["chain_id"]]
        )
        self.assertIn("never completed", self.notices[-1])

    def test_sweep_includes_stuck_notices(self):
        data, _ = self.start(with_funding())
        self.commit_without_continuing()

        result = maintenance.sweep(self.in_minutes(11))

        self.assertEqual(result["stuck_notified"], [data["chain_id"]])


class ChainStatusTests(SweepTestCase):
    def test_lists_active_chains_with_where_they_are(self):
        data, _ = self.start(with_funding())
        self.approve()

        result = maintenance.status()

        self.assertEqual(result["count"], 1)
        row = result["chains"][0]
        self.assertEqual(row["chainId"], data["chain_id"])
        self.assertEqual(row["childId"], CHILD)
        self.assertEqual((row["committed"], row["total"]), (1, 3))
        self.assertIn("Plan 2 of 3", row["waitingOn"])
        self.assertEqual(row["pendingPreviewId"], self.last_preview_id())
        self.assertEqual(row["planIds"], ["plan-1"])

    def test_filters_by_child_and_hides_finished_chains(self):
        data, _ = self.start(with_funding())

        self.assertEqual(maintenance.status("somebody-else")["count"], 0)
        self.assertEqual(maintenance.status(CHILD)["count"], 1)
        chains.finish(data["chain_id"], chains.STATUS_STOPPED)
        self.assertEqual(maintenance.status()["count"], 0)


class ChainCancelTests(SweepTestCase):
    def test_no_active_chain(self):
        result = maintenance.cancel(CHILD)

        self.assertFalse(result["cancelled"])
        self.assertEqual(self.notices, [])

    def test_cancel_closes_the_record_posts_what_exists_and_deletes_nothing(self):
        data, _ = self.start(with_funding())
        self.approve()
        plans_before = [dict(p) for p in self.famly.plans]
        posts_before = len(self.famly.posts)

        result = maintenance.cancel(CHILD)

        self.assertTrue(result["cancelled"])
        self.assertEqual(self.chain(data).status, chains.STATUS_CANCELLED)
        self.assertEqual(self.famly.plans, plans_before)  # nothing deleted or changed
        self.assertEqual(len(self.famly.posts), posts_before)  # nothing written
        notice = self.notices[-1]
        self.assertIn("Timeline cancelled", notice)
        self.assertIn("Plans in Famly for this child right now", notice)
        self.assertIn("`plan-1`: from 2026-09-01 to open-ended", notice)
        self.assertIn("open-ended", notice.split("Plans NOT created")[1])

    def test_the_pending_preview_can_no_longer_be_approved(self):
        self.start(with_funding())
        self.approve()
        pending = self.last_preview_id()
        commits_before = len(self.famly.commits)

        maintenance.cancel(CHILD)

        self.assertEqual(self.approve(pending), approval.MSG_ALREADY_REJECTED)
        self.assertEqual(len(self.famly.commits), commits_before)

    def test_it_frees_the_child(self):
        self.start(with_funding())
        maintenance.cancel(CHILD)

        _, status = self.start(with_funding())

        self.assertEqual(status, 200)

    def test_a_famly_read_failure_does_not_stop_the_cancel(self):
        data, _ = self.start(with_funding())

        with mock.patch.object(self.famly, "get", boom):
            result = maintenance.cancel(CHILD)

        self.assertTrue(result["cancelled"])
        self.assertEqual(self.chain(data).status, chains.STATUS_CANCELLED)
        self.assertIn("Could not read", self.notices[-1])


class ChainResumeTests(SweepTestCase):
    def stuck_after_plan_1(self):
        data, _ = self.start(with_funding())
        self.commit_without_continuing()
        return data

    def test_it_posts_the_next_plan_through_the_normal_path(self):
        data = self.stuck_after_plan_1()

        result = maintenance.resume(CHILD)

        self.assertTrue(result["resumed"])
        self.assertEqual(result["index"], 1)
        self.assertEqual(
            self.heading(), "*Plan 2 of 3 (funded, 2026-11-01 to 2026-12-31)*"
        )
        stored = preview_store.get(self.last_preview_id())
        self.assertEqual((stored.chain_id, stored.chain_index), (data["chain_id"], 1))
        self.assertEqual(stored.old_plan_id, "plan-1")
        self.assertFalse(stored.replace_old_plan)

    def test_the_resumed_chain_runs_to_the_end(self):
        data = self.stuck_after_plan_1()
        maintenance.resume(CHILD)

        self.approve()
        self.approve()

        self.assertEqual(self.chain(data).status, chains.STATUS_COMPLETE)
        self.assertEqual(
            [self.sent(c)["to"] for c in self.famly.commits], [None, None, "2027-07-31"]
        )

    def test_refuses_when_the_next_preview_is_already_pending(self):
        self.start(with_funding())
        self.approve()  # plan 2 posted normally
        posted = len(self.previews_posted)

        result = maintenance.resume(CHILD)

        self.assertFalse(result["resumed"])
        self.assertIn("already pending", result["reason"])
        self.assertEqual(len(self.previews_posted), posted)

    def test_an_expired_next_preview_is_not_pending(self):
        self.start(with_funding())
        self.approve()
        self.age(self.last_preview_id())

        self.assertTrue(maintenance.resume(CHILD)["resumed"])

    def test_refuses_when_the_last_plan_is_missing_from_famly(self):
        self.stuck_after_plan_1()
        self.famly.plans.clear()
        posted = len(self.previews_posted)

        result = maintenance.resume(CHILD)

        self.assertFalse(result["resumed"])
        self.assertIn("not in the child's plans", result["reason"])
        self.assertEqual(len(self.previews_posted), posted)

    def test_refuses_when_the_last_plan_is_not_the_latest_starting(self):
        self.stuck_after_plan_1()
        self.famly.plans.append(
            {"id": "surprise", "childId": CHILD, "from": "2030-01-01", "to": None}
        )
        posted = len(self.previews_posted)

        result = maintenance.resume(CHILD)

        self.assertFalse(result["resumed"])
        self.assertIn("latest-starting", result["reason"])
        self.assertEqual(len(self.previews_posted), posted)

    def test_refuses_with_no_active_chain(self):
        self.assertFalse(maintenance.resume(CHILD)["resumed"])

    def test_refuses_when_nothing_has_been_committed(self):
        self.start(with_funding())
        self.age(self.last_preview_id())  # not pending any more, but nothing exists

        result = maintenance.resume(CHILD)

        self.assertFalse(result["resumed"])
        self.assertIn("no plan has been committed", result["reason"])

    def test_it_writes_nothing_to_famly_itself(self):
        self.stuck_after_plan_1()
        commits = len(self.famly.commits)

        maintenance.resume(CHILD)

        self.assertEqual(len(self.famly.commits), commits)

    def test_with_every_plan_committed_it_completes_the_chain(self):
        data, _ = self.start(payload())
        self.commit_without_continuing()

        result = maintenance.resume(CHILD)

        self.assertTrue(result["completed"])
        self.assertEqual(self.chain(data).status, chains.STATUS_COMPLETE)


class CommandWiringTests(unittest.TestCase):
    def test_the_commands_are_registered_with_main(self):
        import main

        parser = main.build_parser()
        for argv, expected in (
            (["chain-status"], None),
            (["chain-status", "child-1"], "child-1"),
            (["chain-cancel", "child-1"], "child-1"),
            (["chain-resume", "child-1"], "child-1"),
        ):
            self.assertEqual(parser.parse_args(argv).child_id, expected)
        self.assertEqual(parser.parse_args(["chain-sweep"]).action, "chain-sweep")

    def test_cancel_and_resume_require_a_child(self):
        import main

        for command in ("chain-cancel", "chain-resume"):
            with self.assertRaises(SystemExit):
                main.build_parser().parse_args([command])


if __name__ == "__main__":
    unittest.main()
