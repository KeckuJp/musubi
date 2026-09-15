#!/usr/bin/env python3
"""Saved mosquitto_sub %j records only; no broker, subscription or network API."""
import argparse
import csv
import hashlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re

try:
    from scripts.convert_ardupilot_battery_csv import DJI_FORMAT, convert_json, unique_object
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import DJI_FORMAT, convert_json, unique_object

FORMATS = ("vda-state-2.0", "vda-state-2.1", "vda-state-3.0", "ros-battery-json", "farmbot-state", DJI_FORMAT)
LIMIT = 16 * 1024 * 1024


def strict_json(text):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))


def convert(text, topic, payload_format, *, vda_actions=False, vda_errors=False):
    detailed = vda_actions or vda_errors
    if (type(vda_actions) is not bool or type(vda_errors) is not bool
            or detailed and payload_format not in ("vda-state-2.1", "vda-state-3.0")):
        raise ValueError("detail selection requires VDA2.1 or3.0")
    if (payload_format not in FORMATS or not isinstance(topic, str) or not topic
            or any(c in topic for c in "#+\x00") or len(topic.encode()) > 65535):
        raise ValueError("explicit supported topic and payload format required")
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized capture")
    if payload_format == DJI_FORMAT and not re.fullmatch(r"thing/product/[^/]+/osd", topic):
        raise ValueError("explicit DJI OSD topic required")
    output, writer, previous = io.StringIO(newline=""), None, None
    for source_index, line in enumerate(text.splitlines()):
        if not line.strip() or len(line.encode()) > (1024 * 1024 if detailed else 60000):
            raise ValueError("blank or oversized envelope")
        record = strict_json(line)
        if not isinstance(record, dict) or record.get("topic") != topic:
            raise ValueError("capture topic mismatch")
        for name, allowed in (("qos", (0, 1, 2)), ("retain", (0, 1))):
            if type(record.get(name)) is not int or record[name] not in allowed:
                raise ValueError("invalid reported MQTT flag")
        if record["qos"] and (type(record.get("mid")) is not int or not 1 <= record["mid"] <= 65535):
            raise ValueError("invalid reported packet identifier")
        payload = record.get("payload")
        if (not isinstance(payload, str) or type(record.get("payloadlen")) is not int
                or record["payloadlen"] != len(payload.encode("utf-8"))):
            raise ValueError("payload representation or byte count mismatch")
        timestamp = record.get("tst")
        if not isinstance(timestamp, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}[+-]\d{4}", timestamp):
            raise ValueError("unsupported capture timestamp")
        instant = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
        elapsed = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
        capture = (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
        if not 0 <= capture <= 2**63 - 1 or (previous is not None and capture < previous):
            raise ValueError("invalid or decreasing capture clock")
        previous = capture
        # Reformat only for the existing JSONL parser; the exact envelope retains original payload bytes.
        payload_object = strict_json(payload)
        if payload_format in ("farmbot-state", DJI_FORMAT):
            # BotState has no measurement timestamp: use the recorded envelope,
            # not a fabricated payload clock or a required second wrapper.
            payload_object = {"capture_time_us": capture, "payload": payload_object}
        normalized = convert_json(json.dumps(payload_object, ensure_ascii=False, allow_nan=False), payload_format,
                                  vda_actions=vda_actions, vda_errors=vda_errors)
        previous_limit = csv.field_size_limit(2 * 1024 * 1024) if detailed else None
        try:
            rows = list(csv.DictReader(io.StringIO(normalized)))
        finally:
            if previous_limit is not None:
                csv.field_size_limit(previous_limit)
        if not rows or (not detailed and payload_format != DJI_FORMAT and len(rows) != 1):
            raise ValueError("payload did not yield one source observation")
        envelope_digest = "sha256:" + hashlib.sha256(line.encode()).hexdigest() if detailed or payload_format == DJI_FORMAT else None
        for index, row in enumerate(rows):
            if payload_format == DJI_FORMAT:
                row.update(mqtt_source_record_index=source_index, mqtt_envelope_sha256=envelope_digest)
            if detailed:
                row.update(mqtt_source_record_index=source_index,
                           mqtt_envelope_sha256=envelope_digest,
                           mqtt_payload_source_hex=row["source_record_hex"] if index == 0 else "",
                           vda_source_record_index=source_index,
                           vda_source_binding_basis="normalized payload retained in mqtt_payload_source_hex; envelope index")
            row.update(payload_record_time_us=row["record_time_us"], record_time_us=capture,
                       source_record_hex="hex:" + line.encode().hex() if index == 0 else "",
                       mqtt_topic_hex="hex:" + topic.encode().hex(),
                       mqtt_qos=record["qos"], mqtt_retain=record["retain"], mqtt_clock_basis="CAPTURE_REPORTED")
            if writer is None:
                writer = csv.DictWriter(output, fieldnames=list(row), lineterminator="\n")
                writer.writeheader()
            writer.writerow(row)
            if output.tell() > LIMIT:
                raise ValueError("converted capture exceeds bound")
    if writer is None:
        raise ValueError("no captured records")
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--payload-format", choices=FORMATS, required=True)
    parser.add_argument("--vda-actions", action="store_true")
    parser.add_argument("--vda-errors", action="store_true")
    args = parser.parse_args()
    try:
        result = convert(args.input.read_text(encoding="utf-8"), args.topic, args.payload_format,
                         vda_actions=args.vda_actions, vda_errors=args.vda_errors)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(result)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        parser.exit(2, "Saved MQTT conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
