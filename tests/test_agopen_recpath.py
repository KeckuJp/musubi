"""Actual shared-reader RecPath check, selected only after the CI reader build."""
import unittest
from tests import test_gpx_recorded


class RecPathCommonTests(unittest.TestCase):
    def test_saved_recpath_points_reach_common_and_reuse(self):
        # Keep the same assertions with the saved-converter fixtures, without exposing
        # a reader-required test to the earlier stdlib-only contracts invocation.
        test_gpx_recorded.GpxRecordedTests.check_saved_recpath_points_reach_common_and_reuse(self)
