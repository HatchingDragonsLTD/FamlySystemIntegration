"""Phone failure reasons (fixed codes), the log line that carries them, and the read-only
onboard-phone-audit. Numbers are built from fake digits; nothing here is a real number."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from actions.onboard import cli_phone_audit as audit
from actions.onboard import phone
from actions.onboard import store
from core.hubspot import HubSpotClient
from tests.test_onboard_dry_run import FakeHubSpot
from tests.test_onboard_phase2 import DEAL, Phase2Case

D = "5" * 10      # ten fake digits


class ReasonCodeTests(unittest.TestCase):
    def reason(self, raw):
        number, reason = phone.explain(raw)
        self.assertIsNone(number)
        self.assertIn(reason, phone.REASONS)
        return reason

    def test_each_reason_code_comes_from_a_built_example(self):
        self.assertEqual(self.reason("   "), phone.REASON_EMPTY)
        self.assertEqual(self.reason(None), phone.REASON_EMPTY)
        self.assertEqual(self.reason("0" + D + " ext"), phone.REASON_LETTERS)
        self.assertEqual(self.reason("0" + D + "/0" + D), phone.REASON_CHARACTERS)
        self.assertEqual(self.reason("0" + D[:8]), phone.REASON_LENGTH)
        self.assertEqual(self.reason("+44" + D[:9]), phone.REASON_LENGTH)
        self.assertEqual(self.reason("44" + D[:8]), phone.REASON_LENGTH)
        self.assertEqual(self.reason("+3" + D[:3]), phone.REASON_LENGTH)
        self.assertEqual(self.reason(D), phone.REASON_PREFIX)
        self.assertEqual(self.reason("+0" + D), phone.REASON_PREFIX)

    def test_what_is_accepted_is_unchanged_and_a_success_has_no_reason(self):
        self.assertEqual(phone.explain("0" + D), ("+44" + D, None))
        self.assertEqual(phone.explain("+1" + D), ("+1" + D, None))
        self.assertEqual(phone.pick_phone({"mobilephone": "0" + D}), ("mobile", "MOBILE", "+44" + D))
        self.assertEqual(phone.pick_phone({"mobilephone": D}), ("mobile", "MOBILE", None))

    def test_pick_phone_detail_names_the_property_and_the_reason(self):
        self.assertEqual(phone.pick_phone_detail({"phone": D}),
                         ("work", "WORK", None, phone.REASON_PREFIX, "phone"))
        self.assertEqual(phone.pick_phone_detail({}), ("none", None, None, None, None))


class LogLineTests(Phase2Case):
    def test_the_log_line_carries_the_code_slot_and_property_and_no_digits_of_the_number(self):
        bad = "09876543"                                        # 0 + 7 digits: wrong length
        self.hub.contacts["c2"]["mobilephone"] = bad
        self.run_through()
        lines = [l for l in self.logs if "phone not normalised" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("reason=phone_wrong_length", lines[0])
        self.assertIn("property=mobilephone", lines[0])
        self.assertIn("slot=1", lines[0])
        self.assertIn(f"deal={DEAL}", lines[0])
        for chunk in ("98765", "9876543", bad):
            self.assertNotIn(chunk, lines[0])
        self.assertTrue(any("phone_unnormalised" in n for n in self.notify.notices))   # warning as before


class PhoneAuditTests(unittest.TestCase):
    def fake(self, phones):
        hub = FakeHubSpot()
        hub.contacts = {"c1": {"email": "a@x.org", **phones[0]}, "c2": {"email": "b@x.org", **phones[1]}}
        return hub

    def test_it_prints_counts_reasons_and_shapes_only(self):
        hub = self.fake([{"mobilephone": "+44 7" + D[:9]}, {"phone": "0" + D[:8] + " ext 5"}])
        out = audit.audit(hub, ["D1"])
        text = json.dumps(out)
        self.assertEqual((out["contacts"], out["accepted"], out["rejected"]), (2, 1, 1))
        self.assertEqual(out["byReason"], {"phone_has_letters": 1})
        self.assertEqual(out["shapesByReason"], {"phone_has_letters": {"NNNNNNNNN AAA N": 1}})
        self.assertEqual(out["dealsWithRejections"], {"D1": ["phone_has_letters"]})
        self.assertNotIn("5555", text)                         # no digits of any number
        self.assertNotIn("@", text)                            # no emails

    def test_shape_replaces_digits_and_letters_and_marks_odd_characters(self):
        self.assertEqual(audit.shape("+44 (0)7" + D[:3] + "-ab\u202a"), "+NN (N)N" + "NNN" + "-AA?")

    def test_ids_file_ignores_blanks_and_comments_dedupes_and_rejects_non_digits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "deals.txt")
            path.write_text("# header\n12\n\n34  # trailing\n12\n", encoding="utf-8")
            self.assertEqual(audit.read_ids(str(path)), ["12", "34"])
            path.write_text("12\nabc\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                audit.read_ids(str(path))

    def test_it_can_only_read(self):
        import inspect
        source = inspect.getsource(audit)
        for forbidden in ("Writer", ".post(", ".patch(", ".put(", ".delete(", "write_deal_properties"):
            self.assertNotIn(forbidden, source)
        self.assertIs(audit.HubSpotClient, HubSpotClient)      # GET-only transport
        self.assertFalse(hasattr(HubSpotClient, "write_deal_properties"))


if __name__ == "__main__":
    unittest.main()
