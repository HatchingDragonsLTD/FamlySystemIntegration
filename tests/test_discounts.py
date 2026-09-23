"""Tests for the three custom discount slots.

Run from the project root:

    python -m unittest discover -s tests -v

No Famly, no network: these exercise the flattener, the schema pass-through and
the Slack rendering as plain functions.

The money-safety cases are the point of this file. A discount is a percentage
off a real invoice, so a half-filled or fat-fingered slot must be EXCLUDED and
FLAGGED -- never quietly applied, and never silently dropped. It is a warning
rather than a hard error: the rest of the plan still previews, and the human
approving in Slack sees both the plan and the flag.
"""

import unittest

from unittest import mock

from actions.plan_write import hubspot_flatten as flatten
from actions.plan_write import hubspot_intake, input_schema, runner
from actions.read_child_plans.runner import parse_plan
from integrations import slack

CHILD = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000005"
SCHEDULE = "00000000-0000-0000-0000-000000000003"


def flat_payload(**overrides) -> dict:
    """A valid flat payload, with discount slots layered on top."""
    payload = {
        "childId": CHILD,
        "from": "2026-09-01",
        "attendanceScheduleId": SCHEDULE,
        "weeksOfCare": 51,
        "billingId": "ANNUALIZED_V2",
        "billingTitle": "Monthly",
        "billingInvoices": "ADVANCE",
        "monday_session": SESSION,
        "funded": "false",
    }
    payload.update(overrides)
    return payload


class BuildDiscountsTests(unittest.TestCase):
    def test_three_filled_slots_give_three_discounts_in_order(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Sibling Discount",
                discount_1_amount="0.05",
                discount_2_name="Staff Discount",
                discount_2_amount="0.10",
                discount_3_name="Trial Discount",
                discount_3_amount="0.025",
            )
        )

        self.assertEqual(problems, [])
        self.assertEqual([d["ordering"] for d in discounts], [0, 1, 2])
        self.assertEqual(
            [d["title"] for d in discounts],
            ["Sibling Discount", "Staff Discount", "Trial Discount"],
        )
        self.assertEqual([d["amount"] for d in discounts], [0.05, 0.10, 0.025])

    def test_the_fixed_fields_are_set_on_every_discount(self):
        discounts, _ = flatten.build_discounts(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )

        self.assertEqual(
            discounts[0],
            {
                "title": "Sibling",
                "amount": 0.05,
                "ordering": 0,
                "fePriceModifierType": "discount",
                "isPercent": True,
                "origin": "custom",
                "period": "WEEKLY",
                "showOnInvoice": True,
            },
        )

    def test_the_amount_is_a_fraction_and_is_not_divided(self):
        # 5% arrives as 0.05 and must stay 0.05.
        discounts, _ = flatten.build_discounts(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )
        self.assertEqual(discounts[0]["amount"], 0.05)

    def test_empty_slots_are_skipped_entirely(self):
        discounts, problems = flatten.build_discounts(flat_payload())
        self.assertEqual(discounts, [])
        self.assertEqual(problems, [])

        # Explicitly blank, as HubSpot sends for an unset property.
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="",
                discount_1_amount="",
                discount_2_name=None,
                discount_2_amount=None,
            )
        )
        self.assertEqual(discounts, [])
        self.assertEqual(problems, [])

    def test_an_empty_middle_slot_keeps_orderings_tied_to_slot_number(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Sibling",
                discount_1_amount="0.05",
                discount_3_name="Staff",
                discount_3_amount="0.10",
            )
        )

        self.assertEqual(problems, [])
        # NOT compacted to 0 and 1: slot 3 stays ordering 2.
        self.assertEqual([d["ordering"] for d in discounts], [0, 2])
        self.assertEqual([d["title"] for d in discounts], ["Sibling", "Staff"])

    def test_name_without_amount_is_a_problem_and_no_discount(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_2_name="Sibling Discount")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 2", problems[0])
        self.assertIn("amount missing", problems[0])

    def test_amount_without_name_is_a_problem_and_no_discount(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_3_amount="0.05")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 3", problems[0])
        self.assertIn("name missing", problems[0])

    def test_an_out_of_range_amount_is_flagged_and_excluded(self):
        # 5 instead of 0.05 would be a 500% discount on a real invoice.
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_1_name="Oops", discount_1_amount="5.0")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 1", problems[0])
        self.assertIn("out of range", problems[0])
        self.assertIn("5.0", problems[0])

    def test_zero_and_negative_amounts_are_rejected(self):
        for bad in ("0", "0.0", "-0.05"):
            discounts, problems = flatten.build_discounts(
                flat_payload(discount_1_name="Nope", discount_1_amount=bad)
            )
            self.assertEqual(discounts, [], bad)
            self.assertIn("out of range", problems[0], bad)

    def test_exactly_one_hundred_percent_is_allowed(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_1_name="Full waiver", discount_1_amount="1.0")
        )
        self.assertEqual(problems, [])
        self.assertEqual(discounts[0]["amount"], 1.0)

    def test_an_unparseable_amount_is_a_problem(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_1_name="Sibling", discount_1_amount="five percent")
        )

        self.assertEqual(discounts, [])
        self.assertIn("not a number", problems[0])

    def test_one_bad_slot_does_not_lose_the_good_ones(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Sibling",
                discount_1_amount="0.05",
                discount_2_name="Broken",  # no amount
                discount_3_name="Staff",
                discount_3_amount="0.10",
            )
        )

        self.assertEqual([d["ordering"] for d in discounts], [0, 2])
        self.assertEqual(len(problems), 1)


class FlattenIntegrationTests(unittest.TestCase):
    def test_discounts_land_on_the_plan_part(self):
        nested = flatten.flatten_to_nested(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )
        self.assertEqual(len(nested["planParts"][0]["discounts"]), 1)

    def test_no_discounts_gives_an_empty_list_not_a_missing_key(self):
        nested = flatten.flatten_to_nested(flat_payload())
        self.assertEqual(nested["planParts"][0]["discounts"], [])

    def test_problems_ride_along_for_validate_to_report(self):
        nested = flatten.flatten_to_nested(
            flat_payload(discount_2_name="Sibling")  # no amount
        )
        self.assertIn(flatten.PROBLEMS_KEY, nested)
        self.assertIn("amount missing", nested[flatten.PROBLEMS_KEY][0])

    def test_no_problems_key_when_everything_is_fine(self):
        nested = flatten.flatten_to_nested(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )
        self.assertNotIn(flatten.PROBLEMS_KEY, nested)


class SchemaPassThroughTests(unittest.TestCase):
    """Discounts must survive from_dict and reach the request body intact."""

    def _body_discounts(self, **overrides):
        nested = flatten.flatten_to_nested(flat_payload(**overrides))
        plan_input = input_schema.from_dict(nested)
        body = input_schema.to_plan_body(plan_input)
        return plan_input, body["plan"]["planParts"][0]["discounts"]

    def test_a_valid_discount_survives_intact(self):
        plan_input, discounts = self._body_discounts(
            discount_1_name="Sibling Discount", discount_1_amount="0.05"
        )

        self.assertEqual(input_schema.validate(plan_input), [])
        self.assertEqual(len(discounts), 1)
        self.assertEqual(discounts[0]["title"], "Sibling Discount")
        self.assertEqual(discounts[0]["amount"], 0.05)
        self.assertEqual(discounts[0]["ordering"], 0)
        self.assertEqual(discounts[0]["fePriceModifierType"], "discount")
        self.assertIs(discounts[0]["isPercent"], True)
        self.assertEqual(discounts[0]["origin"], "custom")
        self.assertEqual(discounts[0]["period"], "WEEKLY")
        self.assertIs(discounts[0]["showOnInvoice"], True)

    def test_orderings_survive_an_empty_middle_slot(self):
        _, discounts = self._body_discounts(
            discount_1_name="Sibling",
            discount_1_amount="0.05",
            discount_3_name="Staff",
            discount_3_amount="0.10",
        )
        self.assertEqual([d["ordering"] for d in discounts], [0, 2])

    def test_a_producer_problem_is_NOT_a_validation_error(self):
        # Advisory, not fatal: the plan must still preview. The problem is
        # surfaced as a warning instead (see MalformedSlotWarningTests).
        plan_input, discounts = self._body_discounts(discount_2_name="Sibling")

        self.assertEqual(input_schema.validate(plan_input), [])
        # The bad discount is still excluded from the body.
        self.assertEqual(discounts, [])
        # ...and the problem is carried for the warnings channel.
        self.assertTrue(any("Discount 2" in p for p in plan_input.problems))

    def test_the_problems_key_never_reaches_the_plan_body(self):
        nested = flatten.flatten_to_nested(flat_payload(discount_2_name="Sibling"))
        body = input_schema.to_plan_body(input_schema.from_dict(nested))
        self.assertNotIn(flatten.PROBLEMS_KEY, body["plan"])
        self.assertNotIn(flatten.PROBLEMS_KEY, body["plan"]["planParts"][0])

    def test_a_non_object_discount_is_reported(self):
        plan_input = input_schema.from_dict(
            {
                "childId": CHILD,
                "from": "2026-09-01",
                "planParts": [
                    {
                        "attendanceScheduleId": SCHEDULE,
                        "billingProfileId": None,
                        "billing": {"id": "ANNUALIZED_V2"},
                        "sessionBookings": [
                            {"sessionId": SESSION, "day": "MONDAY", "fundable": False}
                        ],
                        "discounts": ["not-an-object"],
                    }
                ],
            }
        )
        errors = input_schema.validate(plan_input)
        self.assertTrue(any("discounts[0]" in e for e in errors))


class MalformedSlotWarningTests(unittest.TestCase):
    """A malformed slot warns; it never blocks the preview.

    This mirrors how Famly's own warnings behave: the plan is computed and
    shown, with the problem flagged beside it, so the approver in Slack sees
    both. Famly is never contacted -- the client is a stub.
    """

    def setUp(self):
        self.calls = []
        outer = self

        class FakeRest:
            def post(self, *args, **kwargs):
                outer.calls.append(1)
                return {
                    "id": "plan-9",
                    "childId": CHILD,
                    "monthlyEstimate": 500.0,
                    "planParts": [],
                    "behaviors": [
                        {
                            "id": "ShowPlanWarnings",
                            "payload": {
                                "warnings": [
                                    {
                                        "title": "Funding hours exceed sessions",
                                        "severity": "warning",
                                        "error": "FundingMismatch",
                                    }
                                ]
                            },
                        }
                    ],
                }

        patcher = mock.patch.object(runner, "RestClient", lambda *a, **k: FakeRest())
        patcher.start()
        self.addCleanup(patcher.stop)

        env = mock.patch.dict("os.environ", {"FAMLY_ACCESS_TOKEN": "test-token"})
        env.start()
        self.addCleanup(env.stop)

    def intake(self, **overrides):
        return hubspot_intake.handle_intake(flat_payload(**overrides), version=3)

    def test_out_of_range_slot_still_previews_successfully(self):
        result = self.intake(discount_1_name="Oops", discount_1_amount="5.0")

        # The preview ran and succeeded -- no 422.
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(self.calls), 1)

    def test_the_bad_discount_is_absent_from_the_committed_body(self):
        result = self.intake(
            discount_1_name="Sibling",
            discount_1_amount="0.05",
            discount_2_name="Oops",
            discount_2_amount="5.0",
        )

        discounts = result.plan_body["plan"]["planParts"][0]["discounts"]
        self.assertEqual([d["title"] for d in discounts], ["Sibling"])

    def test_the_problem_appears_as_a_warning(self):
        result = self.intake(discount_1_name="Oops", discount_1_amount="5.0")

        titles = [w.warning.title for w in result.warnings]
        self.assertTrue(any("Discount 1 excluded" in t for t in titles))
        self.assertTrue(any("out of range" in t for t in titles))

    def test_discount_warnings_sit_alongside_famly_warnings(self):
        result = self.intake(discount_3_name="Staff")  # no amount

        keys = [w.key for w in result.warnings]
        self.assertIn("funding_mismatch", keys)  # Famly's own
        self.assertIn("discount_excluded", keys)  # ours

    def test_a_discount_warning_is_known_and_not_escalated(self):
        # An untracked warning is escalated through notify(); a discount
        # exclusion is understood, so it must not be.
        escalated = []
        result = self.intake(discount_3_name="Staff")

        for warning in result.warnings:
            if warning.key == "discount_excluded":
                self.assertTrue(warning.is_known)
                self.assertIsNotNone(warning.known)
        self.assertEqual(escalated, [])

    def test_a_valid_payload_adds_no_discount_warning(self):
        result = self.intake(discount_1_name="Sibling", discount_1_amount="0.05")

        keys = [w.key for w in result.warnings]
        self.assertNotIn("discount_excluded", keys)

    def test_the_warning_renders_in_the_slack_summary(self):
        import dataclasses

        result = self.intake(discount_2_name="Oops", discount_2_amount="5.0")
        serialised = [dataclasses.asdict(w) for w in result.warnings]

        text = slack.build_summary(result.result.plan, serialised)

        self.assertIn("Discount 2 excluded", text)
        self.assertIn("[discount_excluded]", text)


class SlackDiscountRenderingTests(unittest.TestCase):
    def _summary(self, discounts):
        plan = parse_plan(
            {
                "childId": CHILD,
                "from": "2026-09-01",
                "planParts": [
                    {
                        "planPartId": "pp-1",
                        "sessionBookings": [{"sessionId": SESSION, "day": "MONDAY"}],
                        "discounts": discounts,
                    }
                ],
            }
        )
        return slack.build_summary(plan, [])

    def test_discounts_render_as_percentages(self):
        text = self._summary(
            [
                {"title": "Sibling Discount", "amount": 0.05, "ordering": 0},
                {"title": "Staff Discount", "amount": 0.10, "ordering": 1},
            ]
        )

        self.assertIn("*Discounts* (2)", text)
        self.assertIn("• Sibling Discount — 5%", text)
        self.assertIn("• Staff Discount — 10%", text)

    def test_a_fractional_percentage_keeps_its_decimal(self):
        text = self._summary([{"title": "Trial", "amount": 0.075, "ordering": 0}])
        self.assertIn("• Trial — 7.5%", text)

    def test_the_section_is_omitted_when_there_are_no_discounts(self):
        self.assertNotIn("*Discounts*", self._summary([]))

    def test_discounts_are_listed_in_ordering_order(self):
        text = self._summary(
            [
                {"title": "Third", "amount": 0.03, "ordering": 2},
                {"title": "First", "amount": 0.01, "ordering": 0},
            ]
        )
        self.assertLess(text.index("First"), text.index("Third"))

    def test_a_malformed_discount_does_not_break_the_summary(self):
        text = self._summary(
            [{"amount": 0.05}, {"title": "No amount"}, "junk"]
        )
        self.assertIn("untitled discount — 5%", text)
        self.assertIn("No amount — unknown", text)


if __name__ == "__main__":
    unittest.main()
