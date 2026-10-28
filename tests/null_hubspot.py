"""A HubSpot deal writer that does nothing, for tests whose code path ends in the plan flag / plan status step.

After a commit (or a failed one) the server starts a background step that writes to the deal. Tests that run
those paths must never reach the real `HubSpotDealWriter` (a developer's .env holds a live token): the shared
harnesses patch it with this one. Tests that assert on those writes patch in their own fake afterwards."""

from unittest import mock

from actions.plan_write import plan_flag, status_sync


class NullWriter:
    def read_plan_uploaded(self, deal_id):
        return None

    def read_plan_status_state(self, deal_id):
        return None, None, None

    def deal_property_definitions(self):
        return {}

    def write_plan_uploaded(self, deal_id, value):
        return None

    def write_plan_status(self, deal_id, value):
        return None

    def write_plan_note(self, deal_id, note):
        return None


def patch_all(test_case) -> None:
    """Start the patches for the test and stop them when it ends."""
    for target in (plan_flag, status_sync):
        patcher = mock.patch.object(target, "HubSpotDealWriter", NullWriter)
        patcher.start()
        test_case.addCleanup(patcher.stop)
