"""CLI wiring for delete-resume -- part of the TEMPORARY delete_plans action.

Clears the delete_plans pause flag (set after 3 consecutive failed children) and
prints the current state: the mode, whether it is paused, how many children have
been done today, and the last five delete_log rows. Logic lives in runner.py.

It clears ONLY delete_plans' own flag; the plan queue's pause (`queue-resume`)
is a separate flag and is not touched.
"""

import argparse

from . import runner

NAME = "delete-resume"
HELP = "Clear the delete_plans pause flag and show its state (TEMPORARY action)"


def add_args(parser: argparse.ArgumentParser) -> None:
    pass


def handle(args: argparse.Namespace):
    return runner.resume()
