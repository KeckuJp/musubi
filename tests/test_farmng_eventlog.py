"""Authored saved EventLog containers; no farm-ng runtime, device or real record."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts.export_farmng_eventlog_filter_state import (
    BRIDGE_LIMIT, FIELD_TYPES, PAYLOAD_LIMIT, SELECTED_MESSAGES,
    export_filter_state_events, selected_messages)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/export_farmng_eventlog_filter_state.py"
BRIDGE = ROOT / "scripts/convert_rosbag_observations.py"
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))
EVENT, STATE = selected_messages()
PATH = "/filter/state"
QUERY = "type=farm_ng.filter.proto.FilterState&pb=farm_ng/filter/filter.proto&service_name=filter"
CLOCK, SEND, WRITE = "amiga-sim/monotonic", "service/send", "file/write"
SYSTEM_CLOCK = "amiga-sim/system_clock/UTC"
SELECTION = dict(event_path=PATH, clock_name=CLOCK, semantics=SEND)


def future_messages():
    """A later schema with one new Event, FilterState and nested Pose field."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    schema = descriptor_pb2.FileDescriptorProto(
        name="future_farmng.proto", package="future_farmng", syntax="proto3")
    additions = {"Event": ("future_tag", 9, 9), "FilterState": ("future_scalar", 7, 1),
                 "Pose": ("future_note", 9, 9)}
    for name, entries in SELECTED_MESSAGES:
        descriptor = schema.message_type.add(name=name)
        for entry in entries:
            field = descriptor.field.add(name=entry[0], number=entry[1],
                                         label=3 if len(entry) > 3 and entry[3] else 1)
            if entry[2] in FIELD_TYPES:
                field.type = FIELD_TYPES[entry[2]]
            else:
                field.type, field.type_name = 11, ".future_farmng." + entry[2]
        if name in additions:
            later, number, kind = additions[name]
            descriptor.field.add(name=later, number=number, label=1, type=kind)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return [message_factory.GetMessageClass(pool.FindMessageTypeByName("future_farmng." + name))
            for name in ("Event", "FilterState")]


def event(path=PATH, query=QUERY, payload=b"", seconds=1.5, scheme="protobuf", stamps=None, cls=None):
    message = (cls or EVENT)(payload_length=len(payload))
    message.uri.scheme, message.uri.authority = scheme, "amiga-sim"
    message.uri.path, message.uri.query = path, query
    for stamp, clock, semantics in stamps or [(seconds, CLOCK, SEND), (seconds + .25, CLOCK, WRITE)]:
        message.timestamps.add(stamp=stamp, clock_name=clock, semantics=semantics)
    return message, payload


def record(pair):
    message, payload = pair
    blob = message.SerializeToString()
    return struct.pack("<I", len(blob)) + blob + payload


def state(x=None, parent="world", child="robot", heading=None, flags=None, real=1., cls=None):
    message = (cls or STATE)()
    message.pose.frame_a, message.pose.frame_b = parent, child
    message.pose.a_from_b.rotation.unit_quaternion.real = real
    if x is not None:
        message.pose.a_from_b.translation.x = x
        message.pose.tangent_of_b_in_a.linear_velocity.x = x
        message.pose.tangent_of_b_in_a.angular_velocity.y = .5
        message.uncertainty_diagonal.data.extend([9., 8.])
        message.innovation.data.append(7.)
    if heading is not None:
        message.heading = heading
    if flags is not None:
        message.has_converged, message.is_calibrated = flags
    return message


def payload(**kwargs):
    return state(**kwargs).SerializeToString()


def container(records):
    return b"".join(map(record, records))


def write(directory, blob, name="events.bin"):
    path = Path(directory) / name
    path.write_bytes(blob)
    return path


def bridge_rows(part, directory, name):
    """Existing FilterState bridge and common reader; nothing is reimplemented."""
    source = Path(directory) / f"{name}.jsonl"
    source.write_text(part)
    destination = Path(directory) / name
    subprocess.run([os.sys.executable, str(BRIDGE), str(source), str(destination),
                    "--amiga-filter-json"], check=True, capture_output=True)
    result = subprocess.run([READER, str(PROFILE), str(destination / "observations.csv"),
                             "--allow-equal-time"], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


class FarmngEventLogTests(unittest.TestCase):
    def authored(self):
        """Selected FilterState plus other event types and another FilterState path."""
        return container([
            event("/canbus/twist", "type=farm_ng.canbus.proto.Twist2d&pb=farm_ng/canbus/canbus.proto",
                  b"\x0d\x00\x00\x80\x3f", .5),
            event(payload=payload(x=2., heading=.25, flags=(True, True)), seconds=1.5),
            event(payload=payload(), seconds=2.),
            event("/filter/track", QUERY.replace("FilterState", "FilterTrack"), b"", 2.),
            event(payload=payload(x=-7., parent="map", child="tool"), seconds=2.),
            event("/oak0/rgb", "type=farm_ng.oak.proto.OakFrame&pb=farm_ng/oak/oak.proto", b"\x08\x01", 3.)])

    def test_selected_events_reach_common_observation_with_defaults_and_other_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = self.authored()
            path = write(tmp, blob)
            parts, report = export_filter_state_events(path, **SELECTION)
            self.assertEqual(len(parts), 1)
            self.assertEqual(report["source_records"], 6)
            self.assertEqual(report["selected_records"], 3)
            self.assertEqual(report["unselected_records"], 3)
            self.assertEqual(report["unselected_paths"],
                             {"/canbus/twist": 1, "/filter/track": 1, "/oak0/rgb": 1})
            self.assertEqual(report["truncated_tail_bytes"], 0)
            self.assertTrue(report["complete"])
            self.assertEqual(report["clock"], "configured_capture_not_device_UTC")
            self.assertEqual(report["parts"], [{"records": 3, "bytes": len(parts[0].encode()),
                "sha256": hashlib.sha256(parts[0].encode()).hexdigest()}])
            envelopes = [json.loads(line) for line in parts[0].splitlines()]
            self.assertEqual([item["capture_time_us"] for item in envelopes], [1500000, 2000000, 2000000])
            self.assertEqual([item["source"]["record_index"] for item in envelopes], [1, 2, 4])
            for item in envelopes:
                source = item["source"]
                start, length = source["record_offset"], source["record_bytes"]
                self.assertEqual(bytes.fromhex(source["record_hex"][4:]), blob[start:start + length])
                self.assertEqual(source["record_sha256"], hashlib.sha256(blob[start:start + length]).hexdigest())
            default = envelopes[1]
            self.assertEqual(default["message"]["pose"]["a_from_b"]["translation"], dict(x=0., y=0., z=0.))
            self.assertEqual(default["message"]["pose"]["tangent_of_b_in_a"]["angular_velocity"],
                             dict(x=0., y=0., z=0.))
            self.assertEqual(default["message"]["heading"], 0.)
            self.assertIs(default["message"]["has_converged"], False)
            self.assertEqual(default["message"]["uncertainty_diagonal"], {"data": []})
            self.assertEqual(default["source"]["proto3_absent_fields"],
                             ["has_converged", "is_calibrated", "uncertainty_diagonal",
                              "innovation", "heading"])
            self.assertEqual(envelopes[0]["source"]["proto3_absent_fields"], [])
            self.assertEqual(envelopes[0]["source"]["event_uri"]["query"], QUERY)
            self.assertEqual(envelopes[0]["source"]["capture_time_basis"],
                             "selected_source_clock_microseconds_not_UTC")
            self.assertEqual([stamp["semantics"] for stamp in envelopes[0]["source"]["event_timestamps"]],
                             [SEND, WRITE])

            common = bridge_rows(parts[0], tmp, "observations")
            self.assertEqual(common["main_rows"], 3)
            for row, x in zip(common["observations"], [2., 0., -7.]):
                self.assertEqual(row["fields"]["pose_position_m_x"], x)
                self.assertEqual(row["fields"]["twist_linear_m_s_x"], x)
                self.assertEqual(row["clock_basis"], "Unknown")
                self.assertIsNone(row["anchor_unix_us"])
            self.assertEqual([row["fields"]["heading_rad"] for row in common["observations"]], [.25, 0., 0.])
            self.assertEqual([row["fields"]["filter_converged_reported"] for row in common["observations"]],
                             [1., 0., 0.])
            rows = (Path(tmp) / "observations/observations.csv").read_text().splitlines()[1:]
            for row, original in zip(rows, envelopes):
                self.assertEqual(json.loads(bytes.fromhex(row.split(",")[1][4:])), original)

    def test_second_container_reuses_export_and_keeps_system_clock_unused(self):
        """A system_clock stamp is preserved verbatim but never becomes capture."""
        stamps = [(41.5, CLOCK, SEND), (1789000000.25, SYSTEM_CLOCK, SEND), (41.75, CLOCK, WRITE)]
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, container([
                event(payload=payload(x=3., parent="odom", child="base_link", heading=-.5,
                                      flags=(False, True)), stamps=stamps),
                event("/filter/state/debug", QUERY, payload(x=4.), 42.)]), "second.bin")
            parts, report = export_filter_state_events(path, **SELECTION)
            self.assertEqual((report["selected_records"], report["unselected_records"]), (1, 1))
            self.assertEqual(report["unselected_paths"], {"/filter/state/debug": 1})
            envelope = json.loads(parts[0].strip())
            self.assertEqual(envelope["capture_time_us"], 41500000)
            self.assertEqual(envelope["source"]["capture_timestamp_index"], 0)
            self.assertEqual(envelope["source"]["capture_clock_name"], CLOCK)
            self.assertEqual([stamp["clock_name"] for stamp in envelope["source"]["event_timestamps"]],
                             [CLOCK, SYSTEM_CLOCK, CLOCK])
            self.assertEqual(envelope["message"]["pose"]["frame_a"], "odom")
            self.assertEqual(envelope["units"], {"position": "m", "linear_velocity": "m/s",
                                                 "angular_velocity": "rad/s", "heading": "rad"})
            other, another = export_filter_state_events(
                path, event_path="/filter/state/debug", clock_name=CLOCK, semantics=SEND)
            self.assertEqual(another["selected_records"], 1)
            self.assertEqual(json.loads(other[0].strip())["message"]["pose"]["a_from_b"]["translation"]["x"], 4.)

    def test_unknown_event_and_payload_fields_stay_recoverable_from_original_bytes(self):
        later_event, later_state = future_messages()
        message = state(x=5., heading=.75, cls=later_state)
        message.future_scalar, message.pose.future_note = 6.5, "next"
        blob = message.SerializeToString()
        pair = event(payload=blob, cls=later_event)
        pair[0].future_tag = "later"
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, container([pair]), "future.bin")
            parts, _ = export_filter_state_events(path, **SELECTION)
            source = json.loads(parts[0].strip())["source"]
            self.assertEqual(source["event_top_level_unknown_fields"], 1)
            self.assertEqual(source["payload_top_level_unknown_fields"], 1)
            raw = bytes.fromhex(source["record_hex"][4:])
            length = struct.unpack("<I", raw[:4])[0]
            recovered_event, recovered_state = later_event(), later_state()
            recovered_event.ParseFromString(raw[4:4 + length])
            recovered_state.ParseFromString(raw[4 + length:])
            self.assertEqual(recovered_event.future_tag, "later")
            self.assertEqual(recovered_state.future_scalar, 6.5)
            self.assertEqual(recovered_state.pose.future_note, "next")
            self.assertEqual(recovered_state.heading, .75)
            self.assertEqual(raw[4 + length:], blob)

    def test_mismatch_truncation_oversize_malformed_and_clock_order_reject(self):
        good = record(event(payload=payload(x=1.)))
        oversize = EVENT(payload_length=PAYLOAD_LIMIT + 1)
        oversize.uri.path = "/other"
        cases = [
            (good + b"\x01\x00", "truncated header"),
            (good[:-3], "truncated payload"),
            (struct.pack("<I", 0), "zero length header"),
            (struct.pack("<I", 6) + b"\xff\xff\xff\xff\xff\xff", "malformed event"),
            (record(event(payload=b"\x0a\x05")), "malformed payload"),
            (record((oversize, b"")), "oversized payload"),
            (record(event(query=QUERY.replace("FilterState", "Twist2d"), payload=payload(x=1.))), "type mismatch"),
            (record(event(scheme="grpc", payload=payload(x=1.))), "scheme mismatch"),
            (record(event(payload=payload(x=1.), seconds=2.)) + record(event(payload=payload(x=1.), seconds=1.)),
             "clock decrease"),
            (record(event(payload=payload(x=1.), stamps=[(1., CLOCK, SEND), (2., CLOCK, SEND)])), "ambiguous stamp"),
            (record(event(payload=payload(x=1.), stamps=[(1., CLOCK, WRITE)])), "missing stamp"),
            (record(event(payload=payload(x=1., heading=float("nan")))), "nonfinite heading"),
            (record(event(payload=payload(x=1.), seconds=-1.)), "negative stamp")]
        with tempfile.TemporaryDirectory() as tmp:
            for blob, reason in cases:
                path = write(tmp, blob, "bad.bin")
                with self.assertRaises(ValueError, msg=reason):
                    export_filter_state_events(path, **SELECTION)
            path = write(tmp, self.authored(), "ok.bin")
            with self.assertRaises(ValueError):
                export_filter_state_events(path, event_path="/filter/state/", clock_name=CLOCK, semantics=SEND)
            with self.assertRaises(ValueError):
                export_filter_state_events(path, event_path=PATH, clock_name=CLOCK, semantics="client/send")
            with self.assertRaises(ValueError):
                export_filter_state_events(path, max_part_bytes=BRIDGE_LIMIT + 1, **SELECTION)
            with self.assertRaises(ValueError):
                export_filter_state_events(path, max_part_bytes=64, **SELECTION)
            self.assertEqual(write(tmp, b"\0" * (16 * 1024 * 1024 + 1), "huge.bin").stat().st_size,
                             16 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                export_filter_state_events(Path(tmp) / "huge.bin", **SELECTION)
            truncated = write(tmp, self.authored()[:-4], "tail.bin")
            with self.assertRaises(ValueError):
                export_filter_state_events(truncated, **SELECTION)
            parts, report = export_filter_state_events(truncated, allow_truncated_tail=True, **SELECTION)
            self.assertEqual(report["selected_records"], 3)
            self.assertEqual(report["source_records"], 5)
            self.assertFalse(report["complete"])
            self.assertGreater(report["truncated_tail_bytes"], 0)
            self.assertEqual(len(parts[0].splitlines()), 3)

    def test_ten_thousand_records_reach_common_observation_in_bounded_parts(self):
        """Ordinary capacity through the existing bridge and reader, not export only."""
        with tempfile.TemporaryDirectory() as tmp:
            source = write(tmp, container([event(payload=payload(x=float(index)), seconds=index / 100.)
                                           for index in range(10000)]), "many.bin")
            parts, report = export_filter_state_events(source, **SELECTION)
            self.assertEqual((report["selected_records"], report["unselected_records"]), (10000, 0))
            self.assertGreater(len(parts), 1)
            self.assertEqual(sum(entry["records"] for entry in report["parts"]), 10000)
            self.assertTrue(all(entry["bytes"] <= BRIDGE_LIMIT for entry in report["parts"]))
            rows, indexes = 0, []
            for number, part in enumerate(parts):
                self.assertEqual(hashlib.sha256(part.encode()).hexdigest(),
                                 report["parts"][number]["sha256"])
                envelopes = [json.loads(line) for line in part.splitlines()]
                indexes += [item["source"]["record_index"] for item in envelopes]
                common = bridge_rows(part, tmp, f"part{number}")
                self.assertEqual(common["main_rows"], len(envelopes))
                self.assertEqual([row["fields"]["pose_position_m_x"] for row in common["observations"]],
                                 [float(item["source"]["record_index"]) for item in envelopes])
                rows += common["main_rows"]
            self.assertEqual(rows, 10000)
            self.assertEqual(indexes, list(range(10000)))
            refused = subprocess.run([os.sys.executable, str(SCRIPT), str(source),
                str(Path(tmp) / "single.jsonl"), "--event-path", PATH, "--timestamp-clock", CLOCK,
                "--timestamp-semantics", SEND], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 1)
            self.assertIn(f"{len(parts)} parts are needed", refused.stderr)
            self.assertFalse((Path(tmp) / "single.jsonl").exists())
            batched = Path(tmp) / "many-parts"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(batched), "--parts",
                "--event-path", PATH, "--timestamp-clock", CLOCK, "--timestamp-semantics", SEND],
                check=True, capture_output=True)
            manifest = json.loads((batched / "manifest.json").read_text())
            self.assertEqual(len(manifest["parts"]), len(parts))
            self.assertEqual(manifest["records"], 10000)
            first = 0
            for entry, part in zip(manifest["parts"], parts):
                self.assertEqual(entry["first_record_index"], first)
                self.assertEqual((batched / entry["name"]).read_text(), part)
                first += entry["records"]
            self.assertEqual(first, 10000)

    def test_command_line_parts_manifest_and_refused_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write(tmp, self.authored(), "cli.bin")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            arguments = ["--event-path", PATH, "--timestamp-clock", CLOCK,
                         "--timestamp-semantics", SEND]
            destination = Path(tmp) / "cli.jsonl"
            result = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(destination),
                                     *arguments], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["selected_records"], 3)
            self.assertEqual(destination.read_text(), export_filter_state_events(source, **SELECTION)[0][0])
            for target, reason in ((destination, "cannot create the output (File exists)"),
                                   (source, "output must not be the input file")):
                refused = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                                          *arguments], capture_output=True, text=True)
                self.assertEqual(refused.returncode, 1)
                self.assertEqual(refused.stderr.strip(), "saved EventLog export refused: " + reason)
                self.assertNotIn(tmp, refused.stderr)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertEqual(destination.read_text(), export_filter_state_events(source, **SELECTION)[0][0])

            batched = Path(tmp) / "cli-parts"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(batched), "--parts",
                            *arguments], check=True, capture_output=True)
            manifest = json.loads((batched / "manifest.json").read_text())
            self.assertEqual((manifest["schema"], manifest["records"]),
                             ("farmng-eventlog-filter-state-batch/v1", 3))
            self.assertEqual(manifest["source_sha256"], digest)
            for entry in manifest["parts"]:
                part = (batched / entry["name"]).read_bytes()
                self.assertEqual(entry["first_record_index"], 0)
                self.assertEqual((len(part), hashlib.sha256(part).hexdigest()),
                                 (entry["bytes"], entry["sha256"]))
            refused = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(batched),
                                      "--parts", *arguments], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 1)
            self.assertEqual(json.loads((batched / "manifest.json").read_text()), manifest)


if __name__ == "__main__":
    unittest.main()
