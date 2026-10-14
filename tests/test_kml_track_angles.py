"""Mandatory common-reader stage, not the pre-build stdlib contracts stage."""
import unittest
from tests import test_gpx_recorded


class KmlTrackAngleCommonTests(unittest.TestCase):
    test_selected_track_angles_reach_common = (
        test_gpx_recorded.GpxRecordedTests.check_selected_kml_track_angles_reuse_both_containers_and_common
    )
