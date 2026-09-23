"""Authored documents shaped by iproute2's own JSON key names; no device and no command run."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_gateway_link_state import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/gateway-link-state/profile.toml"


def stats(prefix=0, extra=None):
    body = {"rx": {"bytes": 1000 + prefix, "packets": 10 + prefix, "errors": 1, "dropped": 2,
                   "over_errors": 3, "multicast": 4},
            "tx": {"bytes": 2000 + prefix, "packets": 20 + prefix, "errors": 5, "dropped": 6,
                   "carrier_errors": 7, "collisions": 8}}
    if extra:
        body["rx"].update(extra)
    return body


def link(index=2, name="eth0", kind="stats64", operstate="UP", **overrides):
    entry = {"ifindex": index, "ifname": name, "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
             "mtu": 1500, "operstate": operstate, "link_type": "ether", kind: stats()}
    entry.update(overrides)
    return entry


def document(entries):
    return json.dumps(entries).encode("utf-8")


class GatewayLinkStateTests(unittest.TestCase):
    def test_saved_link_document_reaches_common_output_with_declared_state_and_counters(self):
        raw = document([link(), link(index=3, name="wlan0", operstate="DOWN")])
        output, report = convert(raw, 4242)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["source_links"], report["output_records"]), (2, 2))
        self.assertEqual(rows[0]["link_ifindex"], "2")
        self.assertEqual(bytes.fromhex(rows[0]["link_ifname_hex"][4:]).decode(), "eth0")
        self.assertEqual(rows[0]["link_operstate"], "UP")
        self.assertEqual(rows[1]["link_operstate"], "DOWN")
        self.assertEqual(rows[0]["link_mtu"], "1500")
        self.assertEqual(bytes.fromhex(rows[0]["link_flags_hex"][4:]).decode(),
                         "BROADCAST,MULTICAST,UP,LOWER_UP")
        self.assertEqual((rows[0]["link_rx_bytes"], rows[0]["link_tx_packets"]), ("1000", "20"))
        self.assertEqual(rows[0]["link_rx_over_errors"], "3")
        self.assertEqual(rows[0]["link_tx_carrier_errors"], "7")
        self.assertIn("NOT_A_RATE", rows[0]["link_counter_basis"])
        self.assertIn("SO_NONE_IS_ASSERTED_HERE", rows[0]["link_counter_basis"])
        self.assertIn("NOT_ROUTING_OR_NAT", rows[0]["link_state_basis"])
        self.assertTrue(all(row["record_time_us"] == "4242" for row in rows))
        self.assertNotIn("link_carrier", output)
        self.assertIn("NOT_DECLARED_BY_THIS_WRITER", report["carrier"])

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); path = base / "link.json"; out = base / "out"
            path.write_bytes(raw)
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_gateway_link_state.py"),
                str(path), str(out), "--capture-time-us", "4242"], check=True, capture_output=True)
            self.assertEqual((out / "source.json").read_bytes(), raw)
            reader = os.environ.get("MUSUBI_TELEMETRY_READER")
            if not reader:
                self.skipTest("MUSUBI_TELEMETRY_READER is not configured; common proof NOT RUN")
            common = json.loads(subprocess.run([reader, str(PROFILE),
                str(out / "observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["observations"][0]["fields"]["link_rx_bytes"], 1000)
        self.assertEqual(common["observations"][1]["fields"]["link_tx_collisions"], 8)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_statistics_width_is_explicit_and_never_conflated(self):
        wide, _ = convert(document([link(kind="stats64")]), 1)
        narrow, _ = convert(document([link(kind="stats")]), 1)
        wide_row = next(csv.DictReader(io.StringIO(wide)))
        narrow_row = next(csv.DictReader(io.StringIO(narrow)))
        self.assertEqual((wide_row["link_stats_kind"], narrow_row["link_stats_kind"]),
                         ("stats64", "stats"))
        self.assertEqual(wide_row["link_rx_bytes"], narrow_row["link_rx_bytes"])
        self.assertIn("SIXTY_FOUR_BIT", wide_row["link_stats_basis"])
        self.assertIn("REPRESENTABLE_RANGE_IS_TWO_TO_THE_THIRTY_TWO", narrow_row["link_stats_basis"])
        self.assertNotIn("WRAP", narrow_row["link_stats_basis"])
        both = link()
        both["stats"] = stats()
        with self.assertRaises(ValueError):
            convert(document([both]), 1)

    def test_absent_is_blank_and_counted_while_unknown_keys_are_retained(self):
        bare = {"ifindex": 1, "ifname": "lo"}
        extra = link(index=9, name="eth9", extra=None)
        extra["vendor_future_key"] = {"nested": True}
        extra["stats64"]["rx"]["nohandler"] = 11
        del extra["stats64"]["rx"]["multicast"]
        output, report = convert(document([bare, extra]), 7)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["links_without_statistics"], 1)
        self.assertEqual(rows[0]["link_stats_kind"], "")
        self.assertEqual(rows[0]["link_rx_bytes"], "")
        self.assertEqual(rows[0]["link_mtu"], "")
        for key in ("lo.operstate", "lo.mtu", "lo.flags", "lo.link_type"):
            self.assertIn(key, report["absent_selected_keys"])
        self.assertEqual(rows[1]["link_rx_multicast"], "")
        self.assertIn("eth9.stats64.rx.multicast", report["absent_selected_keys"])
        self.assertIn("eth9.vendor_future_key", report["unmapped_keys"])
        self.assertIn("eth9.stats64.rx.nohandler", report["unmapped_keys"])
        self.assertNotIn("nohandler", output)

    def test_important_failures_are_refused_rather_than_guessed(self):
        for entries in [
            {"ifindex": 2},                                        # not an array
            [],                                                     # empty array
            ["eth0"],                                               # element not an object
            [link(index="2")],                                      # ifindex not an integer
            [link(index=-1)],                                       # negative ifindex
            [link(), link()],                                       # duplicate ifindex
            [link(name=123)],                                       # ifname not a string
            [link(operstate="up")],                                 # lowercase is not emitted
            [link(operstate="ONLINE")],                             # not one of the seven
            [link(mtu="1500")],                                     # string where an integer is due
            [link(mtu=-1)],                                         # negative
            [link(flags="UP")],                                     # flags not an array
            [link(link_type=5)],                                    # link_type not a string
        ]:
            with self.subTest(entries=str(entries)[:60]):
                with self.assertRaises((ValueError, TypeError, KeyError)):
                    convert(document(entries), 1)

        negative = link()
        negative["stats64"]["rx"]["dropped"] = -2
        floated = link()
        floated["stats64"]["tx"]["bytes"] = 1.5
        boolean = link()
        boolean["stats64"]["rx"]["packets"] = True
        wrong_shape = link()
        wrong_shape["stats64"]["rx"] = [1, 2]
        for bad in (negative, floated, boolean, wrong_shape):
            with self.subTest(bad=str(bad)[:50]):
                with self.assertRaises(ValueError):
                    convert(document([bad]), 1)
        conflict = link()
        conflict["operstate_index"] = 9
        with self.assertRaisesRegex(ValueError, "both operstate"):
            convert(document([conflict]), 1)
        unnamed = link()
        del unnamed["operstate"]
        unnamed["operstate_index"] = 9
        row = next(csv.DictReader(io.StringIO(convert(document([unnamed]), 1)[0])))
        self.assertEqual((row["link_operstate"], row["link_operstate_index"]), ("", "9"))
        for raw in (b"", b"not json", b"[" + b"{}" * 10, json.dumps([link()]).encode() * 2):
            with self.subTest(raw=raw[:20]):
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    convert(raw, 1)
        with self.assertRaises(ValueError):
            convert(document([link()]), True)
        with self.assertRaises(ValueError):
            convert(b"[" + document([link()])[1:-1] + b"]" * 1, 1_000_000 * 10**18)



class GatewayLinkStateStrictnessTests(unittest.TestCase):
    """The five faults found in review of 6cae897; each is refused, not resolved silently."""

    def test_duplicate_object_keys_and_non_json_constants_are_refused(self):
        for raw in (b'[{"ifindex": 2, "ifindex": 3, "ifname": "eth0"}]',
                    b'[{"ifindex": 2, "ifname": "eth0", "ifname": "eth1"}]',
                    b'[{"ifindex": 2, "ifname": "eth0", "stats64": {"rx": {"bytes": 1, "bytes": 2}}}]'):
            with self.subTest(raw=raw[:40]):
                with self.assertRaisesRegex(ValueError, "repeats an object key"):
                    convert(raw, 1)
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaisesRegex(ValueError, "non-JSON constant"):
                    convert(b'[{"ifindex": 2, "ifname": "eth0", "mtu": ' + constant + b"}]", 1)

    def test_each_statistics_object_is_held_to_its_own_declared_width(self):
        wide = link(kind="stats")
        wide["stats"]["rx"]["bytes"] = 2**32
        with self.assertRaisesRegex(ValueError, "width the pinned source declares"):
            convert(document([wide]), 1)
        ok = link(kind="stats64")
        ok["stats64"]["rx"]["bytes"] = 2**32
        row = next(csv.DictReader(io.StringIO(convert(document([ok]), 1)[0])))
        self.assertEqual(row["link_rx_bytes"], str(2**32))
        huge = link(kind="stats64")
        huge["stats64"]["tx"]["packets"] = 2**63
        with self.assertRaisesRegex(ValueError, "common signed range"):
            convert(document([huge]), 1)

    def test_identity_and_state_use_the_widths_the_writer_can_emit(self):
        with self.assertRaisesRegex(ValueError, "width the pinned source declares"):
            convert(document([link(index=2**31)]), 1)
        unnamed = link()
        del unnamed["operstate"]
        unnamed["operstate_index"] = 256
        with self.assertRaisesRegex(ValueError, "width the pinned source declares"):
            convert(document([unnamed]), 1)
        unnamed["operstate_index"] = 255
        row = next(csv.DictReader(io.StringIO(convert(document([unnamed]), 1)[0])))
        self.assertEqual(row["link_operstate_index"], "255")
        with self.assertRaisesRegex(ValueError, "width the pinned source declares"):
            convert(document([link(mtu=2**32)]), 1)

    def test_explicit_null_on_a_selected_key_is_malformed_not_a_blank(self):
        for key in ("operstate", "mtu", "flags", "link_type", "stats64"):
            with self.subTest(key=key):
                entry = link()
                entry[key] = None
                with self.assertRaisesRegex(ValueError, "explicit null"):
                    convert(document([entry]), 1)

    def test_unknown_keys_inside_the_statistics_object_are_counted(self):
        entry = link()
        entry["stats64"]["vendor_extra"] = {"x": 1}
        entry["stats64"]["clsact"] = 7
        output, report = convert(document([entry]), 1)
        self.assertIn("eth0.stats64.vendor_extra", report["unmapped_keys"])
        self.assertIn("eth0.stats64.clsact", report["unmapped_keys"])
        self.assertNotIn("vendor_extra", output)
        self.assertIn("GOOD_RECEIVED_BYTES", report["counter_meaning"]["rx_bytes"])
        self.assertIn("if_link.h", report["uapi"])
        self.assertIn("SO_NONE_IS_ASSERTED_HERE", report["counter_basis"])

    def test_two_saved_snapshots_become_a_reported_state_change_through_the_shared_bridge(self):
        """This converter's own output, re-read for the change it contains. No new decoding."""
        from scripts.convert_saved_state_transitions import convert as bridge
        first, _ = convert(document([link(operstate="UP")]), 1_000_000)
        second, _ = convert(document([link(operstate="DOWN")]), 2_000_000)
        saved = (first + second.split("\n", 1)[1]).encode()
        citation = "gateway-link-state/README.md and iproute2 915d3eafcc19706c27b220134b25c24a5b9913b3"
        clock = "us_caller_capture_clock_the_document_carries_no_timestamp"
        output, report = bridge(saved, clock_basis=clock, subjects=("link_ifindex",),
                                states=(("link_operstate", citation),))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(r["record_time_us"], r["transition_disposition"]) for r in rows],
                         [("1000000", "FIRST_REPORTED_VALUE"),
                          ("2000000", "CHANGE_BETWEEN_TWO_REPORTED_VALUES")])
        change = rows[1]
        self.assertEqual(bytes.fromhex(change["previous_state_hex"][4:]).decode(), "UP")
        self.assertEqual(bytes.fromhex(change["reported_state_hex"][4:]).decode(), "DOWN")
        self.assertEqual(json.loads(bytes.fromhex(change["subject_hex"][4:]).decode()), ["2"])
        self.assertEqual((change["previous_record_time_us"], change["elapsed_us"]),
                         ("1000000", "1000000"))
        self.assertEqual(change["declared_clock_basis"], clock)
        self.assertIn("NOT_A_TIMEOUT", change["state_transition_basis"])
        self.assertEqual(report["reported_value_changes"], 1)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "transitions.csv"
                target.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/saved-state-transitions/profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
            fields = common["observations"][1]["fields"]
            self.assertEqual(fields["transition_disposition"], "CHANGE_BETWEEN_TWO_REPORTED_VALUES")
            self.assertEqual(bytes.fromhex(fields["state_column_hex"][4:]).decode(), "link_operstate")
            self.assertIn("not_a_device_channel", common["profile_units"]["state_column_hex"])


if __name__ == "__main__":
    unittest.main()
