#!/usr/bin/env python3
"""Passive export of selected FilterState events from a saved farm-ng EventLog.

The container is the fixed farm-ng-core EventsFileWriter framing at
bc9a04a2e0e792c517eb3192a0980e5652eab19a: every record is a little-endian
uint32 Event length, that many serialized Event bytes, then exactly
Event.payload_length payload bytes.  There is no file header and no sync
marker.  Selected records are re-emitted as the JSONL envelopes that the
existing bridge (convert_rosbag_observations.py --amiga-filter-json) already
accepts, so the meaning of pose/twist/heading is not reinterpreted here.  Each
part therefore stays within that bridge's own 16MiB input bound; --parts splits
at record boundaries when one part is not enough, like the existing bounded
position-CSV batch helper.

Descriptors are rebuilt at runtime from the pinned field numbers with the
pinned optional protobuf decoder; no farm-ng source is bundled or imported and
no service, device or runtime is contacted.
"""
import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

SCHEMA = "farm-ng-amiga/37a8bf217d37a7bc15bb995ca9f13f3c9eebff13/FilterState"
UNITS = {"position": "m", "linear_velocity": "m/s", "angular_velocity": "rad/s", "heading": "rad"}
CORE_COMMIT = "bc9a04a2e0e792c517eb3192a0980e5652eab19a"
SELECTED_TYPE = "farm_ng.filter.proto.FilterState"
SELECTED_PROTO_FILE = "farm_ng/filter/filter.proto"
CONTAINER = "farm-ng-core-EventsFileWriter-uint32le-event-then-payload"
LIMIT = 16 * 1024 * 1024
# A part that the adopted bridge cannot read is not a usable export, so the part
# bound is that bridge's own input bound, not a separate larger number.
BRIDGE_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
EVENT_LIMIT = 64 * 1024
PAYLOAD_LIMIT = 1024 * 1024
DISTINCT_PATHS = 256
BATCH_SCHEMA = "farmng-eventlog-filter-state-batch/v1"
FIELD_TYPES = {"double": 1, "int64": 3, "bool": 8, "string": 9}
# Field numbers copied as facts from the pinned .proto files; the definitions
# themselves stay upstream (Amiga Development Kit License / Apache-2.0).
SELECTED_MESSAGES = (
    ("Vec3F64", (("x", 1, "double"), ("y", 2, "double"), ("z", 3, "double"))),
    ("VecXF64", (("data", 1, "double", True),)),
    ("QuaternionF64", (("imag", 1, "Vec3F64"), ("real", 2, "double"))),
    ("Rotation3F64", (("unit_quaternion", 1, "QuaternionF64"),)),
    ("Isometry3F64", (("rotation", 1, "Rotation3F64"), ("translation", 2, "Vec3F64"))),
    ("Isometry3F64Tangent", (("linear_velocity", 1, "Vec3F64"), ("angular_velocity", 2, "Vec3F64"))),
    ("Pose", (("a_from_b", 1, "Isometry3F64"), ("frame_a", 2, "string"),
              ("frame_b", 3, "string"), ("tangent_of_b_in_a", 4, "Isometry3F64Tangent"))),
    ("FilterState", (("pose", 1, "Pose"), ("has_converged", 2, "bool"), ("is_calibrated", 3, "bool"),
                     ("uncertainty_diagonal", 4, "VecXF64"), ("innovation", 5, "VecXF64"),
                     ("heading", 6, "double"))),
    ("Uri", (("scheme", 1, "string"), ("authority", 2, "string"),
             ("path", 3, "string"), ("query", 4, "string"))),
    ("Timestamp", (("stamp", 1, "double"), ("clock_name", 2, "string"), ("semantics", 3, "string"))),
    ("Event", (("uri", 1, "Uri"), ("timestamps", 2, "Timestamp", True),
               ("payload_length", 3, "int64"), ("sequence", 4, "int64"))),
)
FILTER_STATE_FIELDS = [entry[0] for entry in dict(SELECTED_MESSAGES)["FilterState"]]


def selected_messages():
    """Rebuild the two selected descriptors; no runtime schema loading."""
    try:
        import google.protobuf
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    except ImportError as exc:
        raise ValueError("install pinned optional protobuf decoder") from exc
    if google.protobuf.__version__ != "6.33.5":
        raise ValueError("pinned protobuf decoder required")
    schema = descriptor_pb2.FileDescriptorProto(
        name="selected_farmng.proto", package="selected_farmng", syntax="proto3")
    for name, entries in SELECTED_MESSAGES:
        descriptor = schema.message_type.add(name=name)
        for entry in entries:
            field = descriptor.field.add(name=entry[0], number=entry[1],
                                         label=3 if len(entry) > 3 and entry[3] else 1)
            if entry[2] in FIELD_TYPES:
                field.type = FIELD_TYPES[entry[2]]
            else:
                field.type, field.type_name = 11, ".selected_farmng." + entry[2]
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return [message_factory.GetMessageClass(pool.FindMessageTypeByName("selected_farmng." + name))
            for name in ("Event", "FilterState")]


def finite(value):
    if not math.isfinite(value):
        raise ValueError("nonfinite FilterState value")
    return value


def vector(value):
    return {"x": finite(value.x), "y": finite(value.y), "z": finite(value.z)}


def filter_state_message(state):
    """Every selected scalar is emitted, including proto3 zeros and false."""
    transform, tangent = state.pose.a_from_b, state.pose.tangent_of_b_in_a
    quaternion = transform.rotation.unit_quaternion
    return {
        "pose": {
            "frame_a": state.pose.frame_a, "frame_b": state.pose.frame_b,
            "a_from_b": {"translation": vector(transform.translation),
                         "rotation": {"unit_quaternion": {"imag": vector(quaternion.imag),
                                                          "real": finite(quaternion.real)}}},
            "tangent_of_b_in_a": {"linear_velocity": vector(tangent.linear_velocity),
                                  "angular_velocity": vector(tangent.angular_velocity)}},
        "has_converged": state.has_converged, "is_calibrated": state.is_calibrated,
        "uncertainty_diagonal": {"data": [finite(value) for value in state.uncertainty_diagonal.data]},
        "innovation": {"data": [finite(value) for value in state.innovation.data]},
        "heading": finite(state.heading)}


def query_pairs(query):
    pairs = {}
    for part in query.split("&") if query else []:
        name, separator, value = part.partition("=")
        if not separator or not name or name in pairs:
            raise ValueError("ambiguous event uri query")
        pairs[name] = value
    return pairs


def capture_microseconds(event, clock_name, semantics):
    """One exact clock_name/semantics match; monotonic seconds stay monotonic."""
    matches = [(index, stamp) for index, stamp in enumerate(event.timestamps)
               if stamp.clock_name == clock_name and stamp.semantics == semantics]
    if len(matches) != 1:
        raise ValueError("exactly one selected source timestamp required")
    index, stamp = matches[0]
    seconds = stamp.stamp
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid selected source timestamp")
    microseconds = round(seconds * 1e6)
    if not 0 <= microseconds < 2**63:
        raise ValueError("selected source timestamp exceeds bound")
    return index, microseconds


def bounded_read(path, limit):
    """One bounded read; a file that grows after a stat is never read whole."""
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("saved EventLog input exceeds 16MiB")
    return raw


def export_filter_state_events(path, *, event_path, clock_name, semantics,
                               allow_truncated_tail=False, max_part_bytes=BRIDGE_LIMIT):
    """Saved container only; nothing here connects to a service or device.

    Returns JSONL parts split at record boundaries, each within the adopted
    bridge's own input bound, so every selected record stays consumable.
    """
    if not event_path or not clock_name or not semantics:
        raise ValueError("explicit event path and source clock selection required")
    if type(max_part_bytes) is not int or not 1 <= max_part_bytes <= BRIDGE_LIMIT:
        raise ValueError("invalid part limit")
    raw = bounded_read(path, LIMIT)
    event_class, state_class = selected_messages()
    from google.protobuf import unknown_fields
    from google.protobuf.message import DecodeError
    parts, current, current_bytes, total_bytes = [], [], 0, 0
    position, total, selected, previous, tail, unselected = 0, 0, 0, None, 0, {}
    while position < len(raw):
        start = position
        if len(raw) - position < 4:
            tail = len(raw) - position
            break
        event_length = struct.unpack("<I", raw[position:position + 4])[0]
        if not 0 < event_length <= EVENT_LIMIT:
            raise ValueError("invalid EventLog record header length")
        position += 4
        if len(raw) - position < event_length:
            tail = len(raw) - start
            break
        event = event_class()
        try:
            event.ParseFromString(raw[position:position + event_length])
        except DecodeError as exc:
            raise ValueError("malformed EventLog event header") from exc
        position += event_length
        payload_length = event.payload_length
        if not 0 <= payload_length <= PAYLOAD_LIMIT:
            raise ValueError("invalid or oversized EventLog payload length")
        if len(raw) - position < payload_length:
            tail = len(raw) - start
            break
        payload = raw[position:position + payload_length]
        position += payload_length
        total += 1
        if event.uri.path != event_path:
            # Unselected records stay counted and undecoded, never dropped silently.
            unselected[event.uri.path] = unselected.get(event.uri.path, 0) + 1
            if len(unselected) > DISTINCT_PATHS:
                raise ValueError("saved EventLog exceeds distinct path bound")
            continue
        pairs = query_pairs(event.uri.query)
        if (event.uri.scheme != "protobuf" or pairs.get("type") != SELECTED_TYPE
                or pairs.get("pb") != SELECTED_PROTO_FILE):
            raise ValueError("selected event path carries an unselected message type")
        state = state_class()
        try:
            state.ParseFromString(payload)
        except DecodeError as exc:
            raise ValueError("malformed FilterState payload") from exc
        stamp_index, microseconds = capture_microseconds(event, clock_name, semantics)
        if previous is not None and microseconds < previous:
            raise ValueError("selected source clock decreases")
        previous = microseconds
        present = {field.name for field, _ in state.ListFields()}
        record = raw[start:position]
        envelope = {
            "schema": SCHEMA, "units": dict(UNITS), "capture_time_us": microseconds,
            "message": filter_state_message(state),
            "source": {
                "core_commit": CORE_COMMIT, "container": CONTAINER,
                "record_index": total - 1, "record_offset": start,
                "record_bytes": len(record),
                "record_sha256": hashlib.sha256(record).hexdigest(),
                # The whole original record, length prefix included, so any field
                # this decoder does not know stays recoverable byte for byte.
                "record_hex": "hex:" + record.hex(),
                "event_uri": {"scheme": event.uri.scheme, "authority": event.uri.authority,
                              "path": event.uri.path, "query": event.uri.query},
                "event_sequence_reported": event.sequence,
                "event_payload_length": payload_length,
                "event_timestamps": [{"stamp": finite(stamp.stamp), "clock_name": stamp.clock_name,
                                      "semantics": stamp.semantics} for stamp in event.timestamps],
                "capture_timestamp_index": stamp_index,
                "capture_clock_name": clock_name, "capture_clock_semantics": semantics,
                "capture_time_basis": "selected_source_clock_microseconds_not_UTC",
                "proto3_absent_fields": [name for name in FILTER_STATE_FIELDS if name not in present],
                # Only the outermost unknown fields are counted; nested ones are
                # not, and neither count replaces record_hex for recovery.
                "event_top_level_unknown_fields": len(unknown_fields.UnknownFieldSet(event)),
                "payload_top_level_unknown_fields": len(unknown_fields.UnknownFieldSet(state))}}
        line = json.dumps(envelope, allow_nan=False) + "\n"
        length = len(line.encode("utf-8"))
        if length > max_part_bytes:
            raise ValueError("single record exceeds the part limit")
        total_bytes += length
        if total_bytes > OUTPUT_LIMIT:
            raise ValueError("saved EventLog export exceeds 128MiB")
        if current_bytes + length > max_part_bytes:
            parts.append("".join(current))
            current, current_bytes = [], 0
        current.append(line)
        current_bytes += length
        selected += 1
    if current:
        parts.append("".join(current))
    if tail and not allow_truncated_tail:
        raise ValueError("incomplete trailing EventLog record")
    if not selected:
        raise ValueError("no selected FilterState events")
    return parts, {
        "input_bytes": len(raw), "input_sha256": hashlib.sha256(raw).hexdigest(),
        "selected_event_path": event_path, "selected_type": SELECTED_TYPE,
        "selected_clock_name": clock_name, "selected_clock_semantics": semantics,
        "source_records": total, "selected_records": selected,
        "unselected_records": total - selected, "unselected_paths": dict(sorted(unselected.items())),
        "truncated_tail_bytes": tail, "complete": not tail,
        "max_part_bytes": max_part_bytes, "output_bytes": total_bytes,
        "parts": [{"records": part.count("\n"), "bytes": len(part.encode("utf-8")),
                   "sha256": hashlib.sha256(part.encode("utf-8")).hexdigest()} for part in parts],
        "clock": "configured_capture_not_device_UTC",
        "evidence": "passive_saved_container_decode_not_device_acceptance",
        "not_normalized": ["uncertainty_diagonal", "innovation"]}


def write_parts(parts, report, output, batched):
    """Never overwrite: exclusive creation only, and the manifest goes last."""
    if batched:
        # mkdir refuses an existing directory, file or symlink; parents are not created.
        output.mkdir()
        manifest = {"schema": BATCH_SCHEMA, "source_sha256": report["input_sha256"],
                    "records": 0, "parts": []}
        for index, part in enumerate(parts, 1):
            name = f"part-{index:06d}.jsonl"
            with (output / name).open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(part)
            entry = dict(report["parts"][index - 1], name=name,
                         first_record_index=manifest["records"])
            manifest["parts"].append(entry)
            manifest["records"] += entry["records"]
        marker = output / "manifest.json"
        created = False
        try:
            with marker.open("x", encoding="utf-8", newline="\n") as stream:
                created = True
                stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        except OSError:
            if created:
                marker.unlink(missing_ok=True)
            raise
        return
    if len(parts) != 1:
        raise ValueError(f"{len(parts)} parts are needed for the 16MiB bridge input bound; use --parts")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(parts[0])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved farm-ng EventLog file; no device is contacted")
    parser.add_argument("output", type=Path,
                        help="new JSONL file for --amiga-filter-json, or with --parts a new directory")
    parser.add_argument("--event-path", required=True, help="exact Event uri path to select")
    parser.add_argument("--timestamp-clock", required=True, help="exact Timestamp clock_name to use as capture")
    parser.add_argument("--timestamp-semantics", required=True, help="exact Timestamp semantics to use as capture")
    parser.add_argument("--allow-truncated-tail", action="store_true",
                        help="export complete records and report the remaining partial bytes")
    parser.add_argument("--parts", action="store_true",
                        help="write numbered record-boundary parts and a manifest into a new directory")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() == args.input.resolve():
            raise ValueError("output must not be the input file")
        parts, report = export_filter_state_events(
            args.input, event_path=args.event_path, clock_name=args.timestamp_clock,
            semantics=args.timestamp_semantics, allow_truncated_tail=args.allow_truncated_tail)
        write_parts(parts, report, args.output, args.parts)
    except OSError as exc:
        # Fixed reason only; the refused path is the operator's own argument.
        print(f"saved EventLog export refused: cannot create the output ({exc.strerror})", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"saved EventLog export refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
