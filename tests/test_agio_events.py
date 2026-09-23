"""Authored AgIO event text reproducing the pinned writer frame; no real log or device."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_agio_events import CLASS_BASIS, CLOCK_BASIS, convert_agio_events

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_agio_events.py"
PROFILE = ROOT / "profiles/declared/agio-saved-events/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))


def event(clock, message, terminator="\r"):
    """Log.EventWriter: HH:mm:ss then '-> ' then the message then CR."""
    return f"{clock}-> {message}{terminator}"


def first_session():
    return (event("07:01:02", "Program Started: Monday, 17 August 2026 07:01")
            + event("07:01:02", "AgIO Version: 6.7.2")
            + event("07:01:03", "UDP Network Is On")
            + event("07:01:04", "UDP Network is connected: 192.168.5.255:9999")
            + event("07:01:05", "Loopback is Connected: 127.0.0.1:17777")
            + event("07:02:00", "UDP Network Failed to Connect")
            + event("07:02:01", "Error in AgIO - UDP: bind refused | Exception: Address in use")
            + event("07:03:00", "Catch -> Load UDP Server System.Exception: boom\r\n   at AgIO.Foo()")
            + "Log trimmed to last 100 lines\r"
            + event("07:04:00", "Program Exit: Monday, 17 August 2026 07:04\n\r"))


def second_session():
    """Different authored input: midnight rollover, CRLF terminators, no exit line."""
    return (event("23:59:58", "Program Started: Sunday, 16 August 2026 23:59", "\r\n")
            + event("23:59:59", "UDP Network Is On", "\r\n")
            + event("00:00:01", "UDP Network Failed to Connect", "\r\n")
            + event("00:00:02", "Using Default Profile At Start Warning", "\r\n")
            + event("00:00:03", "RTCM List compilation error"))


class AgioEventTests(unittest.TestCase):
    def common(self, text, directory, name):
        converted, report = convert_agio_events(text)
        path = Path(directory) / f"{name}.csv"
        path.write_text(converted, encoding="utf-8", newline="")
        rows = list(csv.DictReader(io.StringIO(converted)))
        observations = None
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            observations = json.loads(subprocess.run(
                [READER, str(PROFILE), str(path), "--allow-equal-time"],
                check=True, capture_output=True, text=True).stdout)
        return rows, report, observations

    def test_two_authored_sessions_reach_common_observation_with_retained_text(self):
        with tempfile.TemporaryDirectory() as directory:
            rows, report, common = self.common(first_session(), directory, "first")
            self.assertEqual(report["records"], 9)
            self.assertEqual(report["event_classes"], {
                "ERROR_REPORT": 1, "LOOPBACK_CONNECTED": 1, "SESSION_EXIT": 1, "SESSION_START": 1,
                "UDP_NETWORK_CONNECTED": 1, "UDP_NETWORK_CONNECT_FAILED": 1, "UDP_NETWORK_ON": 1,
                "UNCLASSIFIED_RETAINED": 1, "VERSION_REPORTED": 1})
            self.assertEqual([row["source_line"] for row in rows], [str(n) for n in range(1, 10)])
            self.assertEqual([row["unlocated_time_us"] for row in rows], ["0"] * 9)
            self.assertEqual([row["agio_reported_clock_hhmmss"] for row in rows][:2], ["07:01:02"] * 2)
            self.assertTrue(all(row["agio_reported_clock_basis"] == CLOCK_BASIS for row in rows))
            self.assertEqual(rows[3]["agio_event_class"], "UDP_NETWORK_CONNECTED")
            self.assertEqual(bytes.fromhex(rows[3]["agio_event_detail_hex"][4:]).decode(),
                             "192.168.5.255:9999")
            self.assertEqual(bytes.fromhex(rows[0]["agio_session_date_reported"][4:]).decode(),
                             "Monday, 17 August 2026 07:01")
            self.assertEqual([row["agio_session_date_reported"] for row in rows[1:8]], [""] * 7)
            self.assertEqual(rows[6]["agio_event_class"], "ERROR_REPORT")
            self.assertEqual(bytes.fromhex(rows[6]["agio_event_detail_hex"][4:]).decode(),
                             "AgIO - UDP: bind refused | Exception: Address in use")
            self.assertEqual(rows[7]["agio_event_class"], "UNCLASSIFIED_RETAINED")
            self.assertEqual(rows[7]["agio_continuation_lines"], "2")
            whole = bytes.fromhex(rows[7]["agio_record_hex"][4:]).decode()
            self.assertEqual(whole, "07:03:00-> Catch -> Load UDP Server System.Exception: boom\n"
                                    "   at AgIO.Foo()\nLog trimmed to last 100 lines")
            self.assertEqual(report["upstream_trim_notice_candidates"], 1)
            self.assertEqual(report["continuation_lines"], 2)
            self.assertIn("caller was not located", report["upstream_loss"])
            self.assertIn("not established", report["upstream_loss"])
            self.assertEqual(report["clock"], "Unknown")
            if common:
                self.assertEqual(common["main_rows"], 9)
                self.assertEqual(common["observations"][3]["fields"]["agio_event_class"],
                                 "UDP_NETWORK_CONNECTED")
                for record in common["observations"]:
                    self.assertEqual(record["clock_basis"], "Unknown")
                    self.assertIsNone(record["anchor_unix_us"])

            rows, report, common = self.common(second_session(), directory, "second")
            self.assertEqual(report["records"], 5)
            self.assertEqual([row["agio_reported_clock_hhmmss"] for row in rows],
                             ["23:59:58", "23:59:59", "00:00:01", "00:00:02", "00:00:03"])
            self.assertEqual(report["date_basis"],
                             "SESSION_LINES_ONLY_NO_PER_EVENT_DATE_ZONE_OR_ROLLOVER")
            self.assertNotIn("SESSION_EXIT", report["event_classes"])
            self.assertEqual(report["event_classes"]["UNCLASSIFIED_RETAINED"], 2)
            self.assertEqual(rows[4]["agio_event_class"], "UNCLASSIFIED_RETAINED")
            self.assertEqual(bytes.fromhex(rows[4]["agio_record_hex"][4:]).decode(),
                             "00:00:03-> RTCM List compilation error")
            if common:
                self.assertEqual(common["main_rows"], 5)
                self.assertTrue(all(record["clock_basis"] == "Unknown"
                                    for record in common["observations"]))

    def test_terminators_leading_fragment_and_invalid_input(self):
        mixed = ("07:00:01-> UDP Network Is On\r"
                 "07:00:02-> UDP Network Is On\n"
                 "07:00:03-> UDP Network Is On\r\n"
                 "07:00:04-> UDP Network Is On")
        converted, report = convert_agio_events(mixed)
        self.assertEqual(report["records"], 4)
        self.assertEqual(report["blank_lines"], 0)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(converted)))), 4)
        leading = "   at AgIO.Foo()\rLog trimmed to last 100 lines\r" + event("07:00:05", "UDP Network Is On")
        _, report = convert_agio_events(leading)
        self.assertEqual((report["records"], report["unanchored_leading_lines"]), (1, 2))
        self.assertEqual(report["continuation_lines"], 0)
        self.assertEqual(report["upstream_trim_notice_candidates"], 1)
        self.assertEqual(bytes.fromhex(report["unanchored_leading"][0][4:]).decode(), "   at AgIO.Foo()")
        for bad, reason in (("", "empty"), ("no anchor at all\r", "unanchored only"),
                            ("25:00:00-> UDP Network Is On\r", "invalid hour"),
                            ("07:60:00-> UDP Network Is On\r", "invalid minute"),
                            ("07:00:60-> UDP Network Is On\r", "invalid second"),
                            ("07:00:01->UDP Network Is On\r", "missing frame space"),
                            ("07:00:01-> ok\r\x00", "NUL byte")):
            with self.assertRaises(ValueError, msg=reason):
                convert_agio_events(bad)
        with self.assertRaises(ValueError):
            convert_agio_events("07:00:01-> " + "x" * (16 * 1024 * 1024))

    def test_more_than_ten_thousand_records_through_the_common_reader(self):
        count = 10500
        text = "".join(event(f"{index // 3600 % 24:02d}:{index // 60 % 60:02d}:{index % 60:02d}",
                              f"UDP Network is connected: 192.168.5.255:{9000 + index % 500}")
                       for index in range(count))
        with tempfile.TemporaryDirectory() as directory:
            rows, report, common = self.common(text, directory, "many")
            self.assertEqual(report["records"], count)
            self.assertEqual(report["event_classes"], {"UDP_NETWORK_CONNECTED": count})
            self.assertEqual(len(rows), count)
            self.assertEqual([row["source_line"] for row in rows[:3]], ["1", "2", "3"])
            self.assertEqual(rows[-1]["source_line"], str(count))
            if common:
                self.assertEqual(common["main_rows"], count)
                self.assertEqual([record["fields"]["agio_event_class"]
                                  for record in common["observations"][:2]],
                                 ["UDP_NETWORK_CONNECTED"] * 2)
                self.assertTrue(all(record["clock_basis"] == "Unknown"
                                    for record in common["observations"]))

    def test_every_leading_line_beyond_thirty_two_is_retained_in_order(self):
        """The bound is on the report, not a line-count cut of unknown content."""
        fragments = [f"   at AgIO.Frame{index}()" for index in range(40)]
        fragments[-1] = "   at AgIO.RecognizableLastFrame()"
        text = "".join(line + "\r" for line in fragments) + event("07:00:05", "UDP Network Is On")
        converted, report = convert_agio_events(text)
        self.assertEqual(report["records"], 1)
        self.assertEqual(report["unanchored_leading_lines"], 40)
        self.assertEqual(len(report["unanchored_leading"]), 40)
        self.assertNotIn("unanchored_leading_truncated_in_report", report)
        self.assertEqual([bytes.fromhex(item[4:]).decode() for item in report["unanchored_leading"]],
                         fragments)
        self.assertEqual(bytes.fromhex(report["unanchored_leading"][-1][4:]).decode(),
                         "   at AgIO.RecognizableLastFrame()")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(converted)))), 1)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "leading.txt"
            source.write_text(text, encoding="utf-8", newline="")
            target = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target)],
                           check=True, capture_output=True)
            saved = json.loads((target / "report.json").read_text())
            self.assertEqual(len(saved["unanchored_leading"]), 40)
            self.assertEqual(bytes.fromhex(saved["unanchored_leading"][-1][4:]).decode(),
                             "   at AgIO.RecognizableLastFrame()")

    def test_embedded_timestamp_line_is_kept_whole_and_marked_a_candidate(self):
        """The writer does not escape newlines, so this boundary is an assumption."""
        embedded = ("Error in AgIO - UDP: bind refused | Exception: first\r"
                    "07:05:00-> UDP Network Is On\r"
                    "   at AgIO.Inner()")
        text = event("07:04:59", embedded) + event("07:05:01", "UDP Network Failed to Connect")
        converted, report = convert_agio_events(text)
        rows = list(csv.DictReader(io.StringIO(converted)))
        self.assertEqual(report["records"], 3)
        self.assertEqual([row["agio_event_class"] for row in rows],
                         ["ERROR_REPORT", "UDP_NETWORK_ON", "UDP_NETWORK_CONNECT_FAILED"])
        self.assertEqual(rows[1]["agio_reported_clock_hhmmss"], "07:05:00")
        self.assertEqual(rows[1]["agio_continuation_lines"], "1")
        self.assertEqual(bytes.fromhex(rows[1]["agio_record_hex"][4:]).decode(),
                         "07:05:00-> UDP Network Is On\n   at AgIO.Inner()")
        self.assertEqual(bytes.fromhex(rows[0]["agio_record_hex"][4:]).decode(),
                         "07:04:59-> Error in AgIO - UDP: bind refused | Exception: first")
        whole = "\n".join(bytes.fromhex(row["agio_record_hex"][4:]).decode() for row in rows)
        for line in ("Exception: first", "UDP Network Is On", "   at AgIO.Inner()"):
            self.assertIn(line, whole)
        self.assertTrue(all(row["agio_event_class_basis"] == CLASS_BASIS for row in rows))
        self.assertIn("not a certified boundary", report["record_boundary_basis"])
        self.assertEqual(report["class_basis"], CLASS_BASIS)
        self.assertIn("CANDIDATE", CLASS_BASIS)
        self.assertNotIn("trims to the last 100 lines", report["upstream_loss"])
        self.assertIn("caller was not located", report["upstream_loss"])
        self.assertIn("clear the buffer when a save fails", report["upstream_loss"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "embedded.csv"
            path.write_text(converted, encoding="utf-8", newline="")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([READER, str(PROFILE), str(path),
                    "--allow-equal-time"], check=True, capture_output=True, text=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                for record in common["observations"]:
                    self.assertEqual(record["fields"]["agio_event_class_basis"], CLASS_BASIS)

    def test_command_line_writes_a_new_directory_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "events.txt"
            source.write_text(first_session(), encoding="utf-8", newline="")
            target = Path(directory) / "out"
            result = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target)],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["records"], 9)
            self.assertEqual((target / "observations.csv").read_text(),
                             convert_agio_events(first_session())[0])
            self.assertEqual(json.loads((target / "report.json").read_text())["clock"], "Unknown")
            failed = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target)],
                                    capture_output=True)
            self.assertNotEqual(failed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
