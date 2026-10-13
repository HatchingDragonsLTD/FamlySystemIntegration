"""Tests for the delete_plans end-of-run Slack digest.

Run from the project root:

    python -m unittest tests.test_delete_digest -v

Only three things post at once (an unexpected stop, a tripped pause, a daily-cap
stop); everything else waits for ONE digest. Reuses the delete_plans harness: a
fake Famly, a recorded Slack and a stand-in for the delete that is not written
yet. NOTHING here touches Famly or Slack.
"""

import argparse
import json
import logging
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

try:  # `python -m unittest tests.test_delete_digest`
    from tests import test_delete_plans as base
except ImportError:  # `python -m unittest discover -s tests`
    import test_delete_plans as base

from actions.delete_plans import delete_digest, digest, runner
from actions.plan_write import preview_store
from integrations import slack

logging.disable(logging.CRITICAL)

plan_node = base.plan_node


def cid(n: int) -> str:
    return f"00000000-0000-0000-0000-00000000f{n:03d}"


class DigestTestCase(base.DeleteTestCase):
    def allow(self, *children):
        self.allowlist_file.write_text("\n".join(children) + "\n", encoding="utf-8")

    def give(self, child, *plans):
        for index, plan_id in enumerate(plans):
            self.famly.plans.append(plan_node(plan_id, f"2026-0{index + 1}-01", child=child))

    def digests(self):
        return [n for n in self.notices if "delete_plans digest" in n or n.startswith("(")]

    def cli(self, if_quiet=None):
        self.names_allowed = True
        try:
            return delete_digest.handle(argparse.Namespace(if_quiet=if_quiet))
        finally:
            self.names_allowed = False

    def reported(self):
        with preview_store.open_store() as connection:
            return [dict(r) for r in connection.execute("SELECT id, reported_at FROM delete_log ORDER BY id")]


# --------------------------------------------------------------------------- #
# What posts at once, and what does not
# --------------------------------------------------------------------------- #
class ImmediateMessageTests(DigestTestCase):
    def setUp(self):
        super().setUp()
        self.allow(cid(1), cid(2), cid(3), cid(9))  # cid(9) never reports: no auto digest

    def test_a_routine_deleted_child_posts_nothing(self):
        self.give(cid(1), "a", "b")
        self.request(child=cid(1))

        self.assertEqual(self.notices, [])

    def test_nothing_to_delete_posts_nothing(self):
        self.request(child=cid(1))

        self.assertEqual(self.notices, [])

    def test_a_partly_deleted_child_posts_nothing(self):
        self.famly.plans.append(plan_node("a", "2026-01-01", child=cid(1), deletable=False))
        self.request(child=cid(1))

        self.assertEqual(self.notices, [])

    def test_a_refused_child_posts_nothing(self):
        self.request(child=base.OTHER)

        self.assertEqual(self.notices, [])

    def test_a_dry_run_posts_nothing(self):
        self.give(cid(1), "a")
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            self.request(child=cid(1))

        self.assertEqual(self.notices, [])

    def test_a_child_stopped_by_an_existing_pause_posts_nothing(self):
        self.give(cid(1), "a")
        runner.set_paused(True)
        self.request(child=cid(1))

        self.assertEqual(self.notices, [])  # not a NEW pause: the digest reports it

    def test_an_unexpected_result_that_stops_a_child_posts_at_once(self):
        self.give(cid(1), "a", "b")
        self.delete_effect = self.leave_plan_in_place

        self.request(child=cid(1))

        self.assertEqual(len(self.notices), 1)
        self.assertIn("stopped", self.notices[0])
        self.assertIn(cid(1), self.notices[0])

    def test_each_kind_of_unexpected_stop_posts_at_once(self):
        scenarios = {
            "delete raises": lambda: setattr(self, "delete_effect", lambda plan: (_ for _ in ()).throw(RuntimeError("no"))),
            "plan still there": lambda: setattr(self, "delete_effect", self.leave_plan_in_place),
            "read fails": lambda: setattr(self.famly, "get_failures", [RuntimeError("down")]),
        }
        for name, arrange in scenarios.items():
            with self.subTest(name):
                runner.set_paused(False)  # a fresh count each time: three in a row would pause
                self.notices.clear()
                self.famly.plans = []
                self.give(cid(2), "x")
                self.delete_effect = self.remove_plan
                arrange()

                self.request(child=cid(2))

                self.assertEqual(len(self.notices), 1)

    def test_a_failed_backup_posts_at_once(self):
        self.give(cid(1), "a")
        self.backups.parent.mkdir(parents=True, exist_ok=True)
        self.backups.write_text("in the way", encoding="utf-8")

        self.request(child=cid(1))

        self.assertEqual(len(self.notices), 1)
        self.assertIn("NOT deleted", self.notices[0])

    def test_a_tripped_pause_posts_at_once(self):
        children = [cid(1), cid(2), cid(3)]
        for index, child in enumerate(children):
            self.give(child, f"z{index}")
        self.delete_effect = lambda plan: (_ for _ in ()).throw(RuntimeError("no"))

        for child in children:
            self.request(child=child)

        paused = [n for n in self.notices if "paused" in n]
        self.assertEqual(len(paused), 1)
        self.assertEqual(len(self.notices), 4)  # three stops and the one pause
        self.assertTrue(runner.is_paused())

    def test_a_daily_cap_stop_posts_at_once(self):
        self.give(cid(1), "a")
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "0"}):
            self.request(child=cid(1))

        self.assertEqual(len(self.notices), 1)
        self.assertIn("daily limit", self.notices[0])

    def test_immediate_messages_use_ids_not_names(self):
        self.give(cid(1), "a")
        self.delete_effect = self.leave_plan_in_place
        self.request(child=cid(1))  # the lookup helper fails the test if it is called

        self.assertEqual(self.name_lookups, [])


# --------------------------------------------------------------------------- #
# When the digest posts
# --------------------------------------------------------------------------- #
class WhenItPostsTests(DigestTestCase):
    def test_it_posts_once_every_allowlist_child_has_a_row(self):
        self.allow(cid(1), cid(2))
        self.give(cid(1), "a")
        self.give(cid(2), "b")

        self.request(child=cid(1))
        self.assertEqual(self.digests(), [])  # cid(2) has not reported yet

        self.request(child=cid(2))
        self.assertEqual(len(self.digests()), 1)

    def test_not_before_the_allowlist_is_complete(self):
        self.allow(cid(1), cid(2), cid(3))
        self.give(cid(1), "a")
        self.give(cid(2), "b")

        self.request(child=cid(1))
        self.request(child=cid(2))

        self.assertEqual(self.digests(), [])

    def test_a_child_off_the_allowlist_does_not_complete_it(self):
        self.allow(cid(1), cid(2))
        self.request(child=cid(1))
        self.request(child=base.OTHER)  # refused, and not one of the two

        self.assertEqual(self.digests(), [])

    def test_the_digest_lists_every_child_and_is_one_message_for_a_small_run(self):
        self.allow(cid(1), cid(2))
        self.give(cid(1), "a", "b")
        self.request(child=cid(1))
        self.request(child=cid(2))

        (message,) = self.digests()
        self.assertIn(cid(1), message)
        self.assertIn(cid(2), message)
        self.assertNotIn("(1/", message)

    def test_a_webhook_triggered_digest_fetches_no_names(self):
        self.allow(cid(1))
        self.give(cid(1), "a")
        self.names = {cid(1): "Alex Smith"}

        self.request(child=cid(1))  # completes the allowlist: the digest posts

        self.assertEqual(len(self.digests()), 1)
        self.assertEqual(self.name_lookups, [])
        self.assertNotIn("Alex", self.digests()[0])

    def test_the_request_still_returns_what_it_always_did(self):
        self.allow(cid(1), cid(2))
        self.give(cid(1), "a")
        first, _ = self.request(child=cid(1))  # no digest
        self.give(cid(2), "b")
        second, status = self.request(child=cid(2))  # triggers the digest

        self.assertEqual(status, 200)
        self.assertEqual(set(first), set(second))
        self.assertEqual(second["outcome"], "deleted")
        self.assertEqual(
            set(second), {"ok", "childId", "mode", "outcome", "message", "plans", "warnings", "errors"}
        )

    def test_a_digest_failure_never_fails_the_request(self):
        self.allow(cid(1))
        self.give(cid(1), "a")
        with mock.patch.object(digest, "post_if_complete", side_effect=RuntimeError("boom")):
            data, status = self.request(child=cid(1))

        self.assertEqual((status, data["outcome"]), (200, "deleted"))

    def test_off_mode_never_posts_a_digest(self):
        self.allow(cid(1))
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "off"}):
            self.request(child=cid(1))

        self.assertEqual(self.notices, [])

    def test_a_row_arriving_after_a_digest_starts_the_next_one(self):
        self.allow(cid(1), cid(2))
        self.give(cid(1), "a")
        self.give(cid(2), "b")
        self.request(child=cid(1))
        self.request(child=cid(2))  # digest 1: both children
        self.assertEqual(len(self.digests()), 1)

        self.give(cid(1), "c")
        self.request(child=cid(1))  # a new row, after the digest
        self.assertEqual(len(self.digests()), 1)  # cid(2) has no NEW row: not complete

        self.request(child=cid(2))  # nothing to delete: a row
        digests = self.digests()
        self.assertEqual(len(digests), 2)
        self.assertEqual(digests[1].count(cid(1)), 1)  # only the new rows are in digest 2


class RowsReportedOnceTests(DigestTestCase):
    def setUp(self):
        super().setUp()
        self.allow(cid(1), cid(2))

    def run_both(self):
        self.give(cid(1), "a")
        self.request(child=cid(1))
        self.request(child=cid(2))

    def test_a_posted_row_is_stamped_reported(self):
        self.run_both()

        rows = self.reported()
        self.assertTrue(rows)
        self.assertTrue(all(r["reported_at"] for r in rows))

    def test_a_row_is_never_reported_twice(self):
        self.run_both()
        before = len(self.digests())

        again = self.cli()

        self.assertFalse(again["posted"])
        self.assertEqual(len(self.digests()), before)
        self.assertEqual(digest.unreported_rows(), [])

    def test_the_cli_reports_only_what_the_webhook_digest_did_not(self):
        self.give(cid(1), "a")
        self.request(child=cid(1))  # no digest yet
        self.assertTrue(digest.unreported_rows())

        first = self.cli()
        second = self.cli()

        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        self.assertEqual(len(self.digests()), 1)

    def test_two_digests_starting_together_post_the_rows_once(self):
        self.give(cid(1), "a")
        self.request(child=cid(1))
        barrier = threading.Barrier(2)
        results = []

        def go():
            barrier.wait(timeout=10)
            results.append(digest.post_digest(names=False))

        threads = [threading.Thread(target=go) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(r.posted for r in results), [False, True])
        self.assertEqual(len(self.digests()), 1)

    def test_a_stale_snapshot_cannot_claim_rows_another_digest_already_took(self):
        self.give(cid(1), "a")
        self.request(child=cid(1))
        snapshot = digest.unreported_rows()  # both digests read the same rows...

        first = digest._claim(snapshot, self.clock)
        second = digest._claim(snapshot, self.clock)  # ...and race to claim them

        self.assertTrue(first)
        self.assertEqual(second, [])  # the loser gets nothing, so it posts nothing
        self.assertEqual(len({r["reported_at"] for r in self.reported()}), 1)

    def test_a_digest_slack_refuses_gives_its_rows_back(self):
        self.give(cid(1), "a")
        self.request(child=cid(1))

        with mock.patch.object(slack, "post_notice", lambda text: False):
            failed = digest.post_digest(names=False)

        self.assertFalse(failed.posted)
        self.assertTrue(digest.unreported_rows())  # still waiting
        self.assertTrue(self.cli()["posted"])  # and the next try reports them
        self.assertEqual(digest.unreported_rows(), [])

    def test_the_log_gains_reported_at_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE delete_log (id INTEGER PRIMARY KEY AUTOINCREMENT, child_id TEXT NOT NULL, "
                "plan_id TEXT, at TEXT NOT NULL, mode TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT)"
            )
            connection.execute(
                "INSERT INTO delete_log (child_id, at, mode, outcome) VALUES ('c', '2026-10-13T09:00:00+00:00', 'live', 'done')"
            )
            connection.commit()
            connection.close()

            with mock.patch.dict("os.environ", {"PREVIEW_STORE_PATH": str(path)}):
                rows = digest.unreported_rows()

        self.assertEqual(len(rows), 1)  # the old row survives, unreported
        self.assertIsNone(rows[0]["reported_at"])


class IfQuietTests(DigestTestCase):
    def setUp(self):
        super().setUp()
        self.allow(cid(1), cid(2))
        self.give(cid(1), "a")
        self.request(child=cid(1))  # one row at the clock's time; cid(2) has none

    def test_nothing_unreported_posts_nothing(self):
        self.cli()
        before = len(self.notices)

        self.assertFalse(self.cli(if_quiet=10)["posted"])
        self.assertEqual(len(self.notices), before)

    def test_it_waits_until_the_newest_row_is_old_enough(self):
        self.clock += timedelta(minutes=9, seconds=59)

        result = self.cli(if_quiet=10)

        self.assertFalse(result["posted"])
        self.assertIn("under 10 minute(s)", result["reason"])
        self.assertEqual(self.digests(), [])
        self.assertTrue(digest.unreported_rows())  # untouched

    def test_it_posts_at_exactly_the_threshold(self):
        self.clock += timedelta(minutes=10)

        self.assertTrue(self.cli(if_quiet=10)["posted"])

    def test_it_posts_when_older(self):
        self.clock += timedelta(hours=2)

        self.assertTrue(self.cli(if_quiet=10)["posted"])
        self.assertEqual(digest.unreported_rows(), [])

    def test_the_newest_row_decides_not_the_oldest(self):
        self.clock += timedelta(minutes=30)
        self.give(cid(2), "b")
        self.request(child=cid(2))  # a fresh row at the new time -- but this completes the list...

        # ...and posts the digest from the webhook, so make a new unreported row instead.
        digest_count = len(self.digests())
        self.give(cid(1), "c")
        self.request(child=cid(1))
        self.clock += timedelta(minutes=5)

        result = self.cli(if_quiet=10)

        self.assertFalse(result["posted"])
        self.assertEqual(len(self.digests()), digest_count)

    def test_no_threshold_posts_at_once(self):
        self.assertTrue(self.cli()["posted"])

    def test_the_command_takes_the_threshold(self):
        import main

        args = main.build_parser().parse_args(["delete-digest", "--if-quiet", "10"])

        self.assertEqual(args.if_quiet, 10)
        self.assertIsNone(main.build_parser().parse_args(["delete-digest"]).if_quiet)


# --------------------------------------------------------------------------- #
# What it says
# --------------------------------------------------------------------------- #
class ContentTests(DigestTestCase):
    def build_every_category(self):
        deleted, nothing, partial, failed, expected_absent, refused = cid(1), cid(2), cid(3), cid(4), cid(5), cid(6)
        self.allow(deleted, nothing, partial, failed, expected_absent)
        self.give(deleted, "d1", "d2")
        self.famly.plans.append(plan_node("p1", "2026-01-01", child=partial, deletable=False))
        self.famly.plans.append(plan_node("p2", "2026-02-01", child=partial))
        self.give(failed, "f1")

        def effect(plan):
            if plan.id == "f1":
                raise RuntimeError("Famly said no")
            self.remove_plan(plan)

        self.delete_effect = effect
        for child in (deleted, nothing, partial, failed, refused):
            self.request(child=child)
        return {
            "deleted": deleted, "nothing": nothing, "partial": partial,
            "failed": failed, "absent": expected_absent, "refused": refused,
        }

    def posted(self):
        self.names_allowed = True
        try:
            digest.post_digest(names=True)
        finally:
            self.names_allowed = False
        return "\n".join(self.digests())

    def test_every_category_is_present_with_its_children(self):
        ids = self.build_every_category()
        self.names = {ids["deleted"]: "Alex Smith", ids["failed"]: "Sam Jones"}

        text = self.posted()

        for heading in ("*Deleted* (1)", "*Nothing to delete* (1)", "*Partly deleted or skipped* (1)",
                        "*Failed* (1)", "*Refused* (1)", "*Not received* (1)"):
            self.assertIn(heading, text)
        for child in ids.values():
            self.assertIn(f"`{child}`", text)

    def test_the_header_has_the_mode_and_every_count(self):
        self.build_every_category()

        header = self.posted().splitlines()[0]

        self.assertIn("live", header)
        self.assertIn("6 child(ren)", header)
        self.assertIn(
            "1 deleted, 1 nothing to delete, 1 partly deleted or skipped, 1 failed, 1 refused, 1 not received",
            header,
        )

    def test_each_line_shows_the_name_the_id_in_code_and_the_plans_deleted(self):
        ids = self.build_every_category()
        self.names = {ids["deleted"]: "Alex Smith"}

        text = self.posted()

        self.assertIn(f"• Alex Smith (`{ids['deleted']}`) — 2 plan(s) deleted", text)

    def test_a_failed_line_shows_the_reason(self):
        ids = self.build_every_category()

        text = self.posted()

        failed_line = next(l for l in text.splitlines() if ids["failed"] in l)
        self.assertIn("deleting plan f1 failed", failed_line)
        self.assertIn("Famly said no", failed_line)

    def test_a_partial_line_says_how_many_were_skipped(self):
        ids = self.build_every_category()

        line = next(l for l in self.posted().splitlines() if ids["partial"] in l)

        self.assertIn("1 plan(s) deleted; 1 skipped", line)

    def test_a_refused_line_says_why(self):
        ids = self.build_every_category()

        line = next(l for l in self.posted().splitlines() if ids["refused"] in l)

        self.assertIn("not on the allowlist", line)

    def test_a_child_that_sent_no_request_is_listed_as_not_received(self):
        ids = self.build_every_category()

        line = next(l for l in self.posted().splitlines() if ids["absent"] in l)

        self.assertIn("no request received", line)

    def test_it_ends_with_the_backup_folder(self):
        self.build_every_category()

        text = self.posted()

        self.assertEqual(text.splitlines()[-1], f"Backups: `{self.backups}`")

    def test_a_dry_run_lists_the_plans_that_would_be_deleted(self):
        self.allow(cid(1))
        self.famly.plans = [
            plan_node("plan-a", "2026-01-01", "2026-04-01", child=cid(1), estimate=410.5),
            plan_node("plan-b", "2026-04-01", None, child=cid(1), estimate=520.0),
        ]
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            self.request(child=cid(1))  # completes the allowlist: the digest posts

        text = "\n".join(self.digests())
        self.assertIn("dry run", text.splitlines()[0])
        self.assertIn("1 would delete", text.splitlines()[0])
        self.assertIn("*Would delete (dry run)* (1)", text)
        line = next(l for l in text.splitlines() if cid(1) in l)
        self.assertIn("would delete 2 plan(s)", line)
        self.assertIn("2026-04-01 to open-ended (520.0)", line)
        self.assertIn("2026-01-01 to 2026-04-01 (410.5)", line)
        self.assertLess(line.index("2026-04-01 to open"), line.index("2026-01-01 to 2026-04-01"))  # latest first
        self.assertEqual(self.famly.plans[0]["id"], "plan-a")  # nothing was deleted

    def test_a_child_that_ran_twice_shows_its_latest_result(self):
        self.allow(cid(1))
        self.give(cid(1), "a")
        self.allow(cid(1), cid(9))
        self.request(child=cid(1))  # deleted
        self.request(child=cid(1))  # nothing to delete

        digest.post_digest(names=False)
        text = "\n".join(self.digests())

        self.assertIn("*Nothing to delete* (1)", text)
        self.assertNotIn("*Deleted*", text)

    def test_empty_categories_are_left_out(self):
        self.allow(cid(1))
        self.request(child=cid(1))

        text = "\n".join(self.digests())

        self.assertIn("*Nothing to delete* (1)", text)
        self.assertNotIn("*Failed*", text)
        self.assertNotIn("*Refused*", text)


class ChunkingTests(unittest.TestCase):
    def test_a_short_digest_is_one_unnumbered_message(self):
        self.assertEqual(digest.chunk(["one", "two"]), ["one\ntwo"])

    def test_a_long_digest_is_split_into_numbered_messages_within_the_limit(self):
        lines = [f"• line {n:04d} " + "x" * 80 for n in range(200)]

        messages = digest.chunk(lines, limit=1000)

        self.assertGreater(len(messages), 1)
        for index, message in enumerate(messages, start=1):
            self.assertTrue(message.startswith(f"({index}/{len(messages)}) "), message[:20])
            self.assertLessEqual(len(message), 1000)
        joined = "\n".join(messages)
        for n in range(200):
            self.assertEqual(joined.count(f"line {n:04d} "), 1)  # nothing lost, nothing doubled

    def test_lines_are_not_cut_in_half(self):
        lines = [f"line {n:03d} " + "y" * 50 for n in range(100)]

        for message in digest.chunk(lines, limit=500):
            for line in message.splitlines():
                body = line.split(") ", 1)[1] if line.startswith("(") else line
                self.assertTrue(body.endswith("y" * 50), body)

    def test_one_line_longer_than_the_limit_is_still_split(self):
        messages = digest.chunk(["z" * 5000], limit=1000)

        self.assertGreater(len(messages), 1)
        self.assertEqual(sum(m.count("z") for m in messages), 5000)
        self.assertTrue(all(len(m) <= 1000 for m in messages))

    def test_the_default_limit_is_below_slacks_ceiling(self):
        self.assertLessEqual(digest.SLACK_LIMIT, 4000)


class LargeRunTests(DigestTestCase):
    def test_a_big_run_is_split_and_ends_with_the_backup_folder(self):
        children = [f"00000000-0000-0000-0000-{n:012d}" for n in range(1, 91)]
        self.allow(*children)
        for child in children:
            self.request(child=child)  # nothing to delete: a row each; the last completes the list

        messages = self.digests()
        self.assertGreater(len(messages), 1)
        total = len(messages)
        for index, message in enumerate(messages, start=1):
            self.assertTrue(message.startswith(f"({index}/{total}) "))
            self.assertLessEqual(len(message), digest.SLACK_LIMIT)
        everything = "\n".join(messages)
        for child in children:
            self.assertEqual(everything.count(f"`{child}`"), 1)
        self.assertIn("delete_plans digest", messages[0])
        self.assertTrue(messages[-1].rstrip().endswith(f"Backups: `{self.backups}`"))
        self.assertNotIn("Backups:", "\n".join(messages[:-1]))

    def test_every_row_of_a_chunked_digest_is_marked_reported(self):
        children = [f"00000000-0000-0000-0000-{n:012d}" for n in range(1, 91)]
        self.allow(*children)
        for child in children:
            self.request(child=child)

        self.assertEqual(digest.unreported_rows(), [])


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
class NameTests(DigestTestCase):
    def setUp(self):
        super().setUp()
        self.allow(cid(1), cid(2), cid(3))
        self.give(cid(1), "a")
        for child in (cid(1), cid(2)):
            self.request(child=child)

    def test_the_cli_digest_looks_each_name_up_at_digest_time(self):
        self.names = {cid(1): "Alex Smith", cid(2): "Sam Jones"}

        self.cli()

        self.assertCountEqual(self.name_lookups, [cid(1), cid(2), cid(3)])  # cid(3): not received
        text = "\n".join(self.digests())
        self.assertIn(f"Alex Smith (`{cid(1)}`)", text)
        self.assertIn(f"Sam Jones (`{cid(2)}`)", text)

    def test_a_failed_lookup_falls_back_to_the_bare_id(self):
        self.names = {cid(1): "Alex Smith"}  # cid(2) and cid(3) have no name: the helper returns None

        self.cli()

        text = "\n".join(self.digests())
        self.assertIn(f"Alex Smith (`{cid(1)}`)", text)
        lines = {l for l in text.splitlines() if cid(2) in l}
        self.assertEqual(len(lines), 1)
        self.assertTrue(next(iter(lines)).startswith(f"• `{cid(2)}`"))

    def test_the_real_helper_falls_back_on_any_failure(self):
        # The helper is the existing 3-second lookup: it returns None, never raises.
        from integrations import child_lookup

        mock.patch.stopall()
        with mock.patch.object(child_lookup, "_public_client", side_effect=RuntimeError("no token")):
            self.assertIsNone(child_lookup.lookup_child_name(cid(1)))

    def test_a_name_is_escaped_for_slack(self):
        self.names = {cid(1): "Alex <Smith> & Co"}

        self.cli()

        text = "\n".join(self.digests())
        self.assertIn("Alex &lt;Smith&gt; &amp; Co", text)
        self.assertNotIn("<Smith>", text)

    def test_no_names_are_ever_fetched_during_a_webhook_request(self):
        self.names = {cid(1): "Alex Smith"}
        self.name_lookups.clear()
        self.give(cid(3), "z")

        self.request(child=cid(3))  # completes the allowlist: a webhook-triggered digest

        self.assertEqual(self.name_lookups, [])

    def test_names_are_never_stored_in_the_log_or_the_response(self):
        self.names = {cid(1): "Alex Smith", cid(2): "Sam Jones", cid(3): "Robin Lee"}
        responses = [self.request(child=cid(3))[0]]
        self.cli()

        with preview_store.open_store() as connection:
            dumped = json.dumps([tuple(r) for r in connection.execute("SELECT * FROM delete_log")], default=str)
            for table in ("queue_state", "jobs", "chains", "previews"):
                dumped += json.dumps([tuple(r) for r in connection.execute(f"SELECT * FROM {table}")], default=str)
        for name in ("Alex", "Smith", "Sam", "Jones", "Robin", "Lee"):
            self.assertNotIn(name, dumped)
            self.assertNotIn(name, json.dumps(responses))

    def test_a_names_backup_folder_is_never_a_name(self):
        self.names = {cid(1): "Alex Smith"}
        self.give(cid(1), "b")
        self.request(child=cid(1))

        for path in self.backups.rglob("*"):
            self.assertNotIn("Smith", str(path))


# --------------------------------------------------------------------------- #
# The log's terminal rows, and the docs
# --------------------------------------------------------------------------- #
class TerminalRowTests(DigestTestCase):
    def setUp(self):
        super().setUp()
        self.allow(cid(1), cid(2), cid(9))

    def terminal(self, child):
        return [r for r in self.log(child) if r["outcome"] in ("done", "partial", "stopped", "refused")]

    def test_a_clean_child_ends_with_a_done_row_carrying_the_count(self):
        self.give(cid(1), "a", "b")
        self.request(child=cid(1))

        (row,) = self.terminal(cid(1))
        self.assertEqual(row["outcome"], "done")
        self.assertEqual(json.loads(row["detail"])["deleted"], 2)

    def test_a_skip_ends_with_a_partial_row(self):
        self.famly.plans.append(plan_node("a", "2026-01-01", child=cid(1), deletable=False))
        self.request(child=cid(1))

        (row,) = self.terminal(cid(1))
        self.assertEqual((row["outcome"], json.loads(row["detail"])["skipped"]), ("partial", 1))

    def test_an_unexpected_stop_ends_with_a_stopped_row(self):
        self.give(cid(1), "a")
        self.delete_effect = self.leave_plan_in_place
        self.request(child=cid(1))

        (row,) = self.terminal(cid(1))
        self.assertEqual(row["outcome"], "stopped")

    def test_a_pause_or_cap_ends_with_a_refused_row(self):
        self.give(cid(1), "a")
        runner.set_paused(True)
        self.request(child=cid(1))
        (row,) = self.terminal(cid(1))
        self.assertEqual(row["outcome"], "refused")

        runner.set_paused(False)
        self.give(cid(2), "b")
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "0"}):
            self.request(child=cid(2))
        self.assertEqual(self.terminal(cid(2))[0]["outcome"], "refused")

    def test_the_readme_documents_the_digest_and_the_log_outcomes(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        start = readme.index("## `delete_plans`")
        section = readme[start : readme.index("\n## ", start + 1)]

        for fragment in ("delete-digest", "--if-quiet", "Not received", "reported_at"):
            self.assertIn(fragment, section)
        for outcome in ("done", "partial", "stopped", "refused"):
            self.assertIn(f"`{outcome}`", section)


if __name__ == "__main__":
    unittest.main()
