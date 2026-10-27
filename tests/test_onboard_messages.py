"""Plain-English Slack wording: every code the pipeline emits has a sentence, the
staff message carries sentences (never a bare code list), configuration problems go
to a separate admin line, and nothing personal is in any message.
"""

import ast
import re
import unittest
from pathlib import Path

from actions.onboard import messages, notifier, phone, worker
from tests.test_onboard_phase2 import DEAL, PII, Phase2Case

ROOT = Path(__file__).resolve().parents[1]
SCANNED = [
    "actions/onboard/worker.py", "actions/onboard/inputs.py", "actions/onboard/approval.py",
    "actions/onboard/summary_check.py", "actions/onboard/store.py", "actions/onboard/web.py",
    "actions/onboard/hubspot_sync.py", "actions/onboard_dry_run/runner.py",
    "actions/onboard_dry_run/derive.py", "actions/onboard_dry_run/links.py",
]
CODE = re.compile(r"^[a-z][a-z0-9_]*(:[a-z0-9_]*)?$")


def _module_constants(tree) -> dict:
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _emitted_codes() -> set[str]:
    """Every code the pipeline can emit, found by reading the source: arguments of the
    calls and assignments that raise a hold, a warning, a stop, a flag or an outcome.
    A prefix from an f-string is returned as `prefix*`."""
    trees = {p: ast.parse((ROOT / p).read_text(encoding="utf-8")) for p in SCANNED}
    consts: dict = {}
    for tree in trees.values():
        consts.update(_module_constants(tree))

    def resolve(node) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            return [consts[node.id]] if node.id in consts else []
        if isinstance(node, ast.Attribute):
            return [consts[node.attr]] if node.attr in consts else []
        if isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and head.value:
                return [head.value + "*"]
            return []
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "reason":
                    return resolve(value)
            return []
        if isinstance(node, (ast.List, ast.Tuple)):
            return [c for e in node.elts for c in resolve(e)]
        return []

    found: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                args = node.args
                if name in ("InputIncomplete", "Hold", "hold_once") and args:
                    found.update(resolve(args[0]))
                elif name in ("StopJob", "_fail") and len(args) > 1:
                    found.update(resolve(args[1]))
                elif name == "_end" and len(args) > 3:
                    found.update(resolve(args[3]))
                elif name == "_held" and len(args) > 2:
                    found.update(resolve(args[2]))
                elif name in ("append", "extend") and args:
                    receiver = ast.unparse(node.func.value)
                    if any(w in receiver for w in ("warnings", "reports", "holds", "found")):
                        found.update(resolve(args[0]))
                for keyword in node.keywords:
                    if keyword.arg == "last_error_code":
                        found.update(resolve(keyword.value))
            elif isinstance(node, ast.Dict):
                found.update(resolve(node))
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "code" for t in node.targets):
                    found.update(resolve(node.value))
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and node.value.elts:
                found.update(resolve(node.value.elts[0]))
    # Constants that are codes by definition: every flag the summary check can
    # return, and the worker's stop codes (they reach Slack through paths built from
    # variables, which the call scan above cannot follow).
    summary = _module_constants(trees["actions/onboard/summary_check.py"])
    found.update(v for k, v in summary.items() if k.isupper() and "_" in v)
    worker_consts = _module_constants(trees["actions/onboard/worker.py"])
    found.update(worker_consts[k] for k in ("UNKNOWN_OUTCOME", "PLAN_UNSTABLE", "STAGE_NOT_ELIGIBLE"))
    # Status words and non-codes the scan can pick up are not codes.
    return {c for c in found if CODE.match(c.rstrip("*")) or c.endswith(":*")}


# Codes the source builds without a literal the scan can see.
DYNAMIC_EXAMPLES = [
    "child_http_400", "child_http_503", "contact_http_400", "child_graphql_error",
    "contact_graphql_error", "missing:famly_sync_status", "skipped:famly_childid",
    "value_not_offered:famly_sync_status:contacts_linked",
    "write_failed:famly_sync_note",
]
# Codes written to the jobs table or log from places the scan does not read.
STORE_CODES = ["interrupted", "approval_rejected", "approval_request_not_posted",
               "worker_error", "hubspot_writeback_failed", "contact_outcomes"]


class EveryCodeHasASentenceTests(unittest.TestCase):
    def test_every_code_the_pipeline_emits_has_a_translation(self):
        emitted = _emitted_codes()
        self.assertGreater(len(emitted), 40)                       # the scan really found them
        missing = []
        for code in sorted(emitted):
            if code.endswith("*"):
                if not messages.has_prefix_rule(code.rstrip("*")):
                    missing.append(code)
            elif messages.lookup(code) is None:
                missing.append(code)
        self.assertEqual(missing, [], "codes emitted with no plain-English sentence")

    def test_the_dynamic_and_stored_codes_have_a_translation(self):
        for code in DYNAMIC_EXAMPLES + STORE_CODES:
            with self.subTest(code=code):
                self.assertIsNotNone(messages.lookup(code))

    def test_a_staff_sentence_is_a_sentence_not_a_code(self):
        for code, (kind, sentence) in messages._STATIC.items():
            if kind == messages.INFO:
                continue
            with self.subTest(code=code):
                self.assertTrue(sentence.endswith("."), sentence)
                self.assertNotIn("_", sentence)                    # no raw code in the wording
                self.assertGreater(len(sentence.split()), 4)

    def test_the_wording_staff_were_given(self):
        self.assertEqual(messages.lookup("summary_start_mismatch")[1],
                         "Famly shows a different start date from HubSpot. "
                         "Check the child's start date in Famly.")
        self.assertEqual(messages.lookup("deposit_amount_missing")[1],
                         "No deposit amount is set on the deal.")
        self.assertEqual(messages.lookup("no_quote")[1], "No quote is attached to the deal.")
        self.assertIn("could not be put in a standard format",
                      messages.lookup("phone_unnormalised")[1])

    def test_configuration_codes_are_admin_not_staff(self):
        for code in ("value_not_offered:famly_sync_status:contacts_linked",
                     "missing:famly_sync_gaps", "skipped:famly_sync_note",
                     "write_failed:famly_sync_note", "preflight_failed"):
            self.assertEqual(messages.lookup(code)[0], messages.ADMIN, code)


class StaffMessageTests(Phase2Case):
    EXAMPLE = ["created", "summary_start_mismatch", "value_not_offered:famly_sync_status:contacts_linked",
               "deposit_amount_missing", "no_quote",
               "phone_unnormalised"]

    def test_the_staff_message_lists_sentences_and_keeps_admin_codes_out(self):
        text = notifier.notice_text(DEAL, "✅ Child created in Famly.", self.EXAMPLE)
        self.assertIn("*To check:*", text)
        self.assertIn("• Famly shows a different start date from HubSpot.", text)
        self.assertIn("• No deposit amount is set on the deal.", text)
        self.assertIn("• No quote is attached to the deal.", text)
        self.assertNotIn("value_not_offered", text)
        body = text.split("_Codes:")[0]
        for code in ("summary_start_mismatch", "deposit_amount_missing", "phone_unnormalised"):
            self.assertNotIn(code, body)                           # sentences, not codes
        self.assertEqual(text.count("_Codes:"), 1)                 # one small footer

    def test_the_admin_line_is_separate(self):
        admin = notifier.admin_notice_text(DEAL, self.EXAMPLE)
        self.assertIn("HubSpot setup needs attention", admin)
        self.assertIn("value_not_offered", admin)
        self.assertNotIn("Famly shows a different start date", admin)
        self.assertIsNone(notifier.admin_notice_text(DEAL, ["no_quote", "created"]))

    def test_a_finished_job_posts_a_readable_summary_and_a_separate_admin_line(self):
        self.hub.defs["famly_sync_status"] = {"type": "enumeration", "fieldType": "select",
                                              "options": ["something_else"]}
        self.run_through()
        done = [n for n in self.notify.notices if "Child created in Famly" in n]
        self.assertEqual(len(done), 1)
        self.assertIn("2 contact(s) created, 0 already present.", done[0])
        self.assertNotIn("value_not_offered", done[0])
        admin = [n for n in self.notify.notices if n.startswith("Onboarding admin")]
        self.assertEqual(len(admin), 1)
        self.assertIn("value_not_offered:famly_sync_status", admin[0])

    def test_no_message_carries_personal_data(self):
        self.run_through()
        self.hub.contacts["c2"]["mobilephone"] = "not a number"
        self.hub.contacts["c2"]["phone"] = ""
        texts = list(self.notify.notices) + [g[0] for g in self.notify.gates]
        texts.append(notifier.notice_text(DEAL, "x", self.EXAMPLE))
        texts.append(notifier.admin_notice_text(DEAL, self.EXAMPLE))
        for text in texts:
            for value in PII:
                self.assertNotIn(value, text.casefold())


class PhoneNormaliserTests(unittest.TestCase):
    """What the phone normaliser accepts and rejects (Famly accepted +447700900123,
    07700 900123 and +44 7700 900123 in the probe; we send +44 E.164 anyway)."""

    def check(self, table):
        for raw, expected in table.items():
            with self.subTest(raw=raw):
                self.assertEqual(phone.normalise_uk(raw), expected)

    def test_uk_mobiles_in_every_usual_layout_pass(self):
        self.check({
            "07700 900123": "+447700900123", "07700900123": "+447700900123",
            "07700-900-123": "+447700900123", "07700.900.123": "+447700900123",
            "+44 7700 900123": "+447700900123", "+447700900123": "+447700900123",
            "447700900123": "+447700900123", "  07700 900123  ": "+447700900123",
            "0770 090 0123": "+447700900123",
        })

    def test_brackets_and_the_trunk_zero_are_understood(self):
        self.check({
            "(07700) 900123": "+447700900123", "+44 (0)7700 900123": "+447700900123",
            "+44(0)7700900123": "+447700900123", "+44 07700 900123": "+447700900123",
            "0044 (0)7700 900123": "+447700900123",
        })

    def test_the_0044_prefix_passes_cases(self):
        """Folded cases: the_0044_prefix_passes; uk_landlines_pass."""
        for _i, (_p0, _p1, _p2, _p3) in enumerate([
            ("0044 7700 900123", "00447700900123", "+447700900123", "+447700900123"),
            ("020 7946 0000", "(020) 7946 0000", "+442079460000", "+442079460000"),
        ]):
            with self.subTest(case=_i):
                self.check({_p0: _p2, _p1: _p3})

    def test_extensions_two_numbers_and_text_are_rejected(self):
        self.check({
            "07700 900123 ext 5": None, "07700 900123 x5": None, "07700 900123 ext. 12": None,
            "07700 900123 / 07700 900124": None, "07700 900123, 07700 900124": None,
            "tel: 07700 900123": None, "07700 90012A": None, "abc": None, "": None, None: None,
        })

    def test_wrong_length_uk_numbers_are_rejected_not_guessed(self):
        self.check({
            "077009001": None, "0770090012345": None, "7700900123": None,
            "+44 7700": None, "+4477009001": None, "+0044": None, "0877": None,
        })

    def test_other_countries_pass_only_in_plus_form(self):
        self.check({
            "+33 1 23 45 67 89": "+33123456789", "+353 87 123 4567": "+353871234567",
            "+1 (415) 555-2671": "+14155552671",
            "00353871234567": None,        # a 00 prefix is only understood for the UK
            "+0 123 456 7890": None,
        })

    def test_an_invisible_direction_mark_is_rejected(self):
        # Pasted numbers sometimes carry one; it is not guessed past (see the status note).
        self.assertIsNone(phone.normalise_uk("‪07700 900123"))

    def test_the_pick_prefers_mobile_then_work_and_never_falls_through(self):
        self.assertEqual(phone.pick_phone({"mobilephone": "07700 900123", "phone": "020 7946 0000"}),
                         ("mobile", "MOBILE", "+447700900123"))
        self.assertEqual(phone.pick_phone({"phone": "020 7946 0000"}),
                         ("work", "WORK", "+442079460000"))
        # A bad mobile is NOT replaced by the work number: the caller warns instead.
        self.assertEqual(phone.pick_phone({"mobilephone": "bad", "phone": "020 7946 0000"}),
                         ("mobile", "MOBILE", None))
        self.assertEqual(phone.pick_phone({}), ("none", None, None))


if __name__ == "__main__":
    unittest.main()
