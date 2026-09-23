"""Synthetic fixed DM reports; not vehicle or SAE-wide qualification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_dronecan_capture import convert_j1939

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,extended_id,data_hex\n"


class RecordedDiagnostics(unittest.TestCase):
    def test_bam_reuses_dtc_meanings_preserves_all_frames_and_separate_input(self):
        for pgn, source, dtc in (("ca", "80", "45232307"), ("cb", "81", "01000205")):
            text = HEADER + f"10,1cecff{source},200a0002ff{pgn}fe00\n" + \
                f"11,1cebff{source},011be4{dtc}02\n12,1cebff{source},02000003ffffffff\n"
            output, report = convert_j1939(text, bam=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["frames"], report["decoded_frames"], report["observations"]), (3, 3, 2))
            self.assertEqual([r["dtc_index"] for r in rows], ["0", "1"])
            self.assertEqual(rows[1]["spn_reported"], "2")
            self.assertEqual(rows[0]["first_frame_time_us"], "10")
            self.assertEqual(rows[0]["record_time_us"], "12")
            self.assertEqual(len(json.loads(bytes.fromhex(rows[0]["source_frames_hex"][4:]))), 3)
            self.assertEqual(rows[0]["diagnostic_report_kind"],
                             "ACTIVE_DTC_REPORTED" if pgn == "ca" else "PREVIOUS_DTC_REPORTED")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][1]["fields"]["spn_reported"], 2)

    def test_bam_missing_order_overlap_padding_and_mixed_method(self):
        cm = "10,1cecff80,200a0002ffcafe00\n"
        first = "11,1cebff80,0100000100020502\n"
        last = "12,1cebff80,02000083ffffffff\n"
        output, report = convert_j1939(HEADER + cm + first + last, bam=True)
        self.assertEqual(report["observations"], 1)
        self.assertEqual(len(report["unsupported_dtcs"]), 1)
        self.assertEqual(report["decoded_frames"], 3)
        self.assertIn("2", output)
        for body in (cm + first, cm + last, cm + cm, cm.replace("0a0002", "0a0003") + first + last,
                     cm + first + last.replace("ffffffff", "ffffff00"),
                     cm + first + first.replace("11,", "12,") + last):
            with self.assertRaises(ValueError):
                convert_j1939(HEADER + body, bam=True)

    def test_bam_interleaved_sources_and_maximum_packet_boundary(self):
        lines = ["1,1cecff80,200a0002ffcafe00", "1,1cecff81,200a0002ffcbfe00",
                 "2,1cebff81,0100000100020502", "2,1cebff80,0100000300020504",
                 "3,1cebff80,02000003ffffffff", "4,1cebff81,02000003ffffffff"]
        rows = list(csv.DictReader(io.StringIO(convert_j1939(HEADER + "\n".join(lines) + "\n", bam=True)[0])))
        self.assertEqual([r["source_node_id"] for r in rows], ["128", "128", "129", "129"])
        self.assertEqual([r["spn_reported"] for r in rows], ["3", "4", "1", "2"])
        payload = b"\x00\x00" + bytes.fromhex("01000205") * 445  # 1782 bytes, 255 packets.
        cm = bytes([32]) + len(payload).to_bytes(2, "little") + bytes.fromhex("ffffcafe00")
        lines = ["1,1cecff80," + cm.hex()]
        for index in range(255):
            frame = bytes([index + 1]) + payload[index*7:index*7+7].ljust(7, b"\xff")
            lines.append(f"{index+2},1cebff80,{frame.hex()}")
        _, report = convert_j1939(HEADER + "\n".join(lines) + "\n", bam=True)
        self.assertEqual((report["observations"], report["decoded_frames"]), (445, 256))

    def test_dm1_dm2_reuse_semantics_unknown_and_common_output(self):
        text = HEADER + "1,18feca80,1be445232307ffff\n1,18fecb81,000045233f7fffff\n2,18feee80,0000000000000000\n"
        output, report = convert_j1939(text)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["frames"], 3)
        self.assertEqual(report["decoded_frames"], 2)
        self.assertEqual(len(report["unsupported_frames"]), 1)
        self.assertEqual(rows[0]["spn_reported"], "74565")
        self.assertEqual(rows[0]["fmi_reported_name"], "VOLTAGE_ABOVE_NORMAL")
        self.assertEqual(rows[0]["occurrence_count_code"], "7")
        self.assertEqual(rows[0]["diagnostic_report_kind"], "ACTIVE_DTC_REPORTED")
        self.assertEqual(rows[1]["diagnostic_report_kind"], "PREVIOUS_DTC_REPORTED")
        self.assertEqual(rows[1]["j1939_pgn"], "65227")
        self.assertEqual(rows[1]["fmi_reported_name"], "NOT_AVAILABLE")
        self.assertEqual([rows[0][f"{n}_lamp_code"] for n in
            ("malfunction_indicator", "red_stop", "amber_warning", "protect")], ["0","1","2","3"])
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_frames_hex"][4:]))[0][1], "18feca80")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"
                path.write_text(output)
                result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(result["main_rows"], 2)
                self.assertEqual(result["observations"][1]["fields"]["source_node_id"], 129)
                self.assertEqual(result["observations"][0]["fields"]["spn_reported"], 74565)

    def test_reported_dtc_fields_carry_their_declared_meaning_to_the_reader(self):
        """The profile the case prescribes must declare what each reported DM field means.

        The meanings were stated only in the case prose, so a reader reported the DM1/DM2 kind, the SPN,
        the FMI name, the occurrence code, the index/count and the eight lamp and flash codes with no
        declared unit at all - including the ones that must never read as a diagnosis or a fault.
        """
        profile = ROOT / "profiles/declared/dronecan-recorded/profile.toml"
        text = HEADER + "1,18feca80,1be445232307ffff\n" \
                        "2,18fecb81,000045233f7fffff\n" \
                        "3,18feca82,000000000000ffff\n"
        output, report = convert_j1939(text)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["frames"], report["decoded_frames"], report["observations"]), (3, 3, 3))
        self.assertEqual({r["dtc_index"] for r in rows}, {"0"})
        self.assertEqual({r["declared_dtc_count"] for r in rows}, {"1"})
        self.assertEqual([r["diagnostic_report_kind"] for r in rows],
                         ["ACTIVE_DTC_REPORTED", "PREVIOUS_DTC_REPORTED",
                          "ZERO_SPN_REPORTED_NOT_PHYSICAL_HEALTH"])
        self.assertEqual([r["source_node_id"] for r in rows], ["128", "129", "130"])
        self.assertEqual(report["zero_spn_reports"], 1)
        self.assertEqual(report["frames"],
                         report["decoded_frames"] + len(report["unsupported_frames"]))
        self.assertEqual(rows[2]["j1939_pgn"], "65226")
        self.assertEqual([rows[0][f"{n}_flash_code"] for n in
                          ("malfunction_indicator", "red_stop", "amber_warning", "protect")],
                         ["3", "2", "1", "0"])
        unknown, unknown_report = convert_j1939(HEADER + "4,18feca83,1be423071602ffff\n")
        unknown_row = list(csv.DictReader(io.StringIO(unknown)))[0]
        self.assertEqual((unknown_row["fmi_reported"], unknown_row["fmi_reported_name"]),
                         ("22", "UNKNOWN_22"))
        self.assertEqual(unknown_report["zero_spn_reports"], 0)
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("declared-meaning check needs the built reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(profile),
                                                str(path), "--allow-equal-time"],
                                               check=True, capture_output=True).stdout)
        units = common["profile_units"]
        lamps = ("malfunction_indicator", "red_stop", "amber_warning", "protect")
        for column in ("j1939_pgn", "diagnostic_report_kind", "spn_reported", "fmi_reported",
                       "fmi_reported_name", "occurrence_count_code", "dtc_index", "declared_dtc_count",
                       *[f"{name}_{kind}_code" for kind in ("lamp", "flash") for name in lamps]):
            self.assertTrue(units.get(column), column)
        self.assertIn("65226_is_dm1_active_and_65227_is_dm2_previous", units["j1939_pgn"])
        self.assertIn("never_a_musubi_diagnosis_cause_or_safety_verdict", units["diagnostic_report_kind"])
        self.assertIn("not_physical_health", units["diagnostic_report_kind"])
        self.assertIn("qualified_conversion_method_0", units["spn_reported"])
        self.assertIn("not_a_diagnosed_failure", units["fmi_reported_name"])
        self.assertIn("not_a_calibrated_count_of_physical_events", units["occurrence_count_code"])
        self.assertIn("never_summed_across_messages_or_senders", units["declared_dtc_count"])
        for name in lamps:
            self.assertIn("not_an_observed_lamp_or_an_instruction", units[f"{name}_lamp_code"])
            self.assertIn("no_timing_or_actuation_is_implied", units[f"{name}_flash_code"])
        self.assertIn("capture_clock_us", units["record_time_us"])
        self.assertIn("capture_clock_us_not_source_sampling_time", units["first_frame_time_us"])
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(common["observations"][2]["fields"]["diagnostic_report_kind"],
                         "ZERO_SPN_REPORTED_NOT_PHYSICAL_HEALTH")

    def test_method_boundary_zero_report_and_invalid_exports(self):
        output, report = convert_j1939(HEADER + "1,18feca80,000000000000ffff\n2,18feca80,000001000080ffff\n")
        self.assertEqual(report["decoded_frames"], 1)
        self.assertEqual(len(report["unsupported_frames"]), 1)
        self.assertIn("ZERO_SPN_REPORTED_NOT_PHYSICAL_HEALTH", output)
        for body in ("1,18feca80,0000\n", "1,18feca80,0000000000000000\n",
                     "1,ffffffff,000000000000ffff\n", "2,18feca80,000000000000ffff\n1,18feca80,000000000000ffff\n"):
            with self.assertRaises(ValueError):
                convert_j1939(HEADER + body)
        with self.assertRaises(ValueError):
            convert_j1939(HEADER.replace("extended_id","standard_id") + "1,123,000000000000ffff\n")
