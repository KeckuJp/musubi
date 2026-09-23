#!/usr/bin/env python3
"""Saved mosquitto_sub %j or %J records only; no broker, subscription or network API."""
import argparse
import csv
import hashlib
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import re

try:
    from scripts.convert_ardupilot_battery_csv import CONNECTION_FORMATS, DJI_FORMAT, convert_json, unique_object
    from scripts.recorded_units import normalize
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import CONNECTION_FORMATS, DJI_FORMAT, convert_json, unique_object
    from recorded_units import normalize

# The connection payload carries its own ISO 8601 UTC timestamp, so it needs no capture wrapper: the
# envelope clock stays `record_time_us` and the payload clock stays `payload_record_time_us`, as for state.
# The dock reports its own properties on the same `thing/product/{sn}/osd` topic the aircraft uses -- for a
# dock the serial number in the topic is the gateway (station) device. So the topic never decides which
# producer a capture holds, and neither does `mode_code`, which both sides carry. This selector is therefore
# an explicit CALLER declaration that a capture is a dock1 OSD report: it reads the pinned page's meanings
# for a caller who has established the source, and it does not identify the producer itself.
#
# Pin: dji-sdk/Cloud-API-Doc 4ec6b0c7f9472aeb09a0a47949855d19c473ea07, the commit the aircraft battery table
# already uses, `docs/en/60.api-reference/20.dock-to-cloud/00.mqtt/20.dock/00.dock1/00.properties.md` plus
# that section's `00.topic-definition.md`. The tree places these under **dock-to-cloud** and the topic
# definition states the direction "Device > Cloud Server", "uploaded in frequency by device", with the
# properties page adding "Stable frequency data. Device will report in the frequency of 0.5HZ". The saved
# message is therefore the station's own report captured at a broker, and not a cloud-produced artefact.
DOCK_FORMAT = "dji-dock1-osd-4ec6"
FORMATS = ("vda-state-2.0", "vda-state-2.1", "vda-state-3.0", *CONNECTION_FORMATS, "ros-battery-json",
           "farmbot-state", DJI_FORMAT, DOCK_FORMAT)
# Every enum table below is copied from that page. A code the page does not list is retained, never guessed:
# `network_state.quality` alone proves the name cannot be inverted, because it labels BOTH 1 and 2 "Poor".
DOCK_ENUMS = {
    "mode_code": {0: "IDLE", 1: "ON_SITE_DEBUGGING", 2: "REMOTE_DEBUGGING",
                  3: "FIRMWARE_UPGRADE_IN_PROGRESS", 4: "IN_OPERATION"},
    "cover_state": {0: "DISABLE", 1: "ON", 2: "HALF_OPEN", 3: "COVER_STATE_ABNORMAL"},
    "putter_state": {0: "DISABLE", 1: "ON", 2: "HALF_OPEN", 3: "PUTTER_STATE_ABNORMAL"},
    "supplement_light_state": {0: "DISABLE", 1: "ON"},
    "emergency_stop_state": {0: "DISABLE", 1: "ENABLE"},
    "alarm_state": {0: "DISABLE", 1: "ENABLE"},
    "rainfall": {0: "NO_RAIN", 1: "LIGHT_RAIN", 2: "MODERATE_RAIN", 3: "HEAVY_RAIN"},
    "network_type": {1: "FOUR_G", 2: "ETHERNET"},
    "network_quality": {0: "NO_SIGNAL", 1: "POOR", 2: "POOR", 3: "MODERATE", 4: "BETTER", 5: "GOOD"},
    "charge_state": {0: "IDLE", 1: "CHARGING"},
    "online_status": {0: "POWER_OFF", 1: "POWER_ON"},
    "paired": {0: "NOT_PAIRED", 1: "PAIRED"}}
# Flat dock properties: source key -> (column stem, enum table name).
DOCK_FLAT_ENUMS = (("mode_code", "dock_mode", "mode_code"),
                   ("cover_state", "dock_cover_state", "cover_state"),
                   ("putter_state", "dock_putter_state", "putter_state"),
                   ("supplement_light_state", "dock_supplement_light_state", "supplement_light_state"),
                   ("emergency_stop_state", "dock_emergency_stop_state", "emergency_stop_state"),
                   ("alarm_state", "dock_alarm_state", "alarm_state"),
                   ("rainfall", "dock_rainfall", "rainfall"))
# A fixed column schema: the selected properties are optional per record, so every row carries every
# column and an absent property is blank. Without this the first record would freeze the header and a
# later record reporting one more property would be refused instead of read.
DOCK_FIELDS = tuple(
    [stem + suffix for _, stem, _ in DOCK_FLAT_ENUMS for suffix in ("_code", "_reported")]
    + ["dock_environment_temperature_k", "dock_internal_temperature_k",
       "dock_internal_humidity_percent_reported", "dock_wind_speed_m_s",
       "dock_network_type_code", "dock_network_type_reported",
       "dock_network_quality_code", "dock_network_quality_reported",
       "dock_network_rate_kb_s_reported",
       "dock_aircraft_charge_state_code", "dock_aircraft_charge_state_reported",
       "dock_aircraft_charge_capacity_fraction",
       "dock_sub_device_sn_hex", "dock_sub_device_online_code", "dock_sub_device_online_reported",
       "dock_sub_device_paired_code", "dock_sub_device_paired_reported",
       "dock_gateway_sn_hex", "dock_payload_timestamp_ms_reported",
       "dock_payload_clock_basis", "dock_source_declaration", "dock_identity_basis",
       "dock_enum_basis", "dock_value_basis"])
DOCK_IDENTITY_BASIS = ("REPORTED_GATEWAY_AND_SUB_DEVICE_SERIAL_NUMBERS_AS_WRITTEN_NEITHER_AUTHENTICATED_NOR_"
                       "PHYSICALLY_VERIFIED_AND_NO_PLATFORM_DOMAIN_FOLLOWS_BECAUSE_A_STATION_IS_NOT_A_VEHICLE")
DOCK_ENUM_BASIS = ("REPORTED_CODES_WITH_THE_PINNED_PAGES_OWN_LABELS_THE_CODE_IS_AUTHORITATIVE_BECAUSE_THE_"
                   "NETWORK_QUALITY_TABLE_LABELS_BOTH_1_AND_2_POOR_AND_AN_UNLISTED_CODE_IS_RETAINED")
# This selector is a caller declaration, not a detector. The OSD topic shape and `mode_code` are shared
# with the aircraft, so neither identifies the producer, and a payload as sparse as {"data":{"mode_code":0}}
# is a valid contract instance that establishes nothing about its origin. The pinned page defines what a
# GENUINE dock message means; it does not authenticate an arbitrary saved payload. The token is carried in
# every row so the qualification travels into common output rather than living only in documentation.
DOCK_SOURCE_DECLARATION = "CALLER_DECLARED_DOCK1_SOURCE_NOT_AUTOMATIC_PRODUCER_IDENTIFICATION"
DOCK_VALUE_BASIS = ("STATION_STATUS_READ_UNDER_AN_EXPLICIT_CALLER_DECLARATION_THAT_THIS_CAPTURE_IS_A_DOCK1_OSD_"
                    "REPORT_THE_ORIGIN_IS_UNVERIFIED_HERE_BECAUSE_THE_TOPIC_SHAPE_AND_MODE_CODE_ARE_SHARED_WITH_"
                    "THE_AIRCRAFT_AND_NO_FIELD_IDENTIFIES_A_PRODUCER_THE_ONLY_ORIGIN_CHECK_IS_THE_BOUNDED_"
                    "REFUSAL_OF_A_PAYLOAD_CARRYING_THE_AIRCRAFT_BATTERY_ARRAY_WHICH_CATCHES_THAT_ONE_CONCRETE_"
                    "SHAPE_AND_NOT_EVERY_PARTIAL_OR_SHARED_FIELD_PAYLOAD_THE_PINNED_ROUTE_IS_DOCUMENTED_AS_"
                    "DEVICE_PUBLISHED_SO_A_GENUINE_MESSAGE_OF_THIS_KIND_IS_STATION_ORIGIN_AND_NOT_A_CLOUD_SIDE_"
                    "EXPORT_WHILE_THIS_ROW_IS_NOT_A_COMMAND_AND_NOT_A_PHYSICAL_VERIFICATION_OF_CHARGING_OR_WEATHER")
DOCK_CLOCK_BASIS = ("REPORTED_PAYLOAD_MILLISECOND_TIMESTAMP_OF_THE_TIME_THE_MESSAGE_WAS_SENT_WHOSE_CLOCK_THE_"
                    "PINNED_SOURCE_DOES_NOT_STATE_SO_IT_IS_RETAINED_AND_IS_NEVER_THE_ROW_CLOCK")
LIMIT = 16 * 1024 * 1024
# mosquitto 2.0.22 client/sub_client_output.c: `case 'j'` and `case 'J'` call one json_print with
# escaped=true / false. %J embeds the payload as a JSON value, and in a WITH_CJSON build that value is
# cJSON_ParseWithOpts'd and then reprinted by cJSON, so the embedded text is the publisher's
# serialization and NOT the original payload bytes, while `payloadlen` stays the original wire byte
# count. Those two cannot be compared, so this path reports the length and never verifies it. A build
# without cJSON writes the payload bytes verbatim instead, and a saved line does not say which build
# produced it. Envelope selection is explicit; %j is unchanged.
ENVELOPE_FORMATS = ("j", "J")
JSON_VALUE_ENVELOPE = "MOSQUITTO_%J_PAYLOAD_AS_JSON_VALUE_NOT_ORIGINAL_PAYLOAD_BYTES"
JSON_VALUE_LENGTH_BASIS = ("REPORTED_PAYLOADLEN_NOT_VERIFIED_PUBLISHER_MAY_HAVE_RESERIALIZED_THE_PAYLOAD"
                           "_SO_A_BYTE_COUNT_CHECK_WOULD_BE_FALSE")


def strict_json(text):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))


def dock_enum(row, stem, table, value):
    """A reported code plus the pinned page's label; an unlisted code is retained, never guessed."""
    if type(value) is not int or type(value) is bool or not 0 <= value <= 65535:
        raise ValueError("reported dock enumeration must be a nonnegative integer code")
    row[stem + "_code"] = value
    row[stem + "_reported"] = DOCK_ENUMS[table].get(value, "UNKNOWN_CODE_RETAINED")


def dock_number(value, unit, name):
    """One reported numeric property through the shared engineering-unit table."""
    if type(value) not in (int, float) or type(value) is bool:
        raise ValueError(f"reported dock {name} must be a number")
    return normalize(value, unit)[1]


def dock_reported_number(value, name):
    """A reported number kept in the source's own declared label, with no unit conversion applied."""
    if type(value) not in (int, float) or type(value) is bool or not math.isfinite(value):
        raise ValueError(f"reported dock {name} must be a finite number")
    return value


def dock_row(payload):
    """Selected dock-own status of one saved OSD payload; absent properties stay absent.

    Only `mode_code` is required, because the pinned page marks the dock state as reported on the OSD
    topic. Everything else is optional and a missing key leaves its column blank -- `0` is a real
    reported value in every table here (Idle, Disable, No rain, No signal) and never means absence.
    """
    if not isinstance(payload, dict):
        raise ValueError("saved dock envelope requires a JSON object payload")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("saved dock payload requires a data object")
    # One bounded refusal, not a producer detector: an aircraft OSD carrying the battery array is the
    # concrete confusable shape, so it fails loudly instead of being reported as station status. It
    # catches that shape only. A partial or shared-field payload cannot be distinguished here, which is
    # why the origin travels as a caller declaration rather than as a finding.
    battery = data.get("battery")
    if isinstance(battery, dict) and "batteries" in battery:
        raise ValueError("aircraft OSD battery array is not dock station status")
    if "mode_code" not in data:
        raise ValueError("saved dock payload requires the reported dock state")
    row = {field: "" for field in DOCK_FIELDS}
    for key, stem, table in DOCK_FLAT_ENUMS:
        if key in data:
            dock_enum(row, stem, table, data[key])
    for key, column, unit in (("environment_temperature", "dock_environment_temperature_k", "degC"),
                              ("temperature", "dock_internal_temperature_k", "degC"),
                              ("wind_speed", "dock_wind_speed_m_s", "m/s")):
        if key in data:
            row[column] = dock_number(data[key], unit, key)
    if "humidity" in data:
        # The page declares "Relative humidity / %RH" and no range, so the reported percent is kept as
        # written: no fraction is derived and no 0..100 bound is imposed that the source does not state.
        row["dock_internal_humidity_percent_reported"] = dock_reported_number(data["humidity"], "humidity")
    network = data.get("network_state")
    if network is not None:
        if not isinstance(network, dict):
            raise ValueError("reported dock network state must be an object")
        if "type" in network:
            dock_enum(row, "dock_network_type", "network_type", network["type"])
        if "quality" in network:
            dock_enum(row, "dock_network_quality", "network_quality", network["quality"])
        if "rate" in network:
            # Declared "Kilobytes per second / KB/s". No byte base is documented, so none is applied and
            # the number keeps the source's own label rather than becoming bytes per second.
            row["dock_network_rate_kb_s_reported"] = dock_reported_number(network["rate"], "network rate")
    charge = data.get("drone_charge_state")
    if charge is not None:
        if not isinstance(charge, dict):
            raise ValueError("reported dock charge state must be an object")
        if "state" in charge:
            dock_enum(row, "dock_aircraft_charge_state", "charge_state", charge["state"])
        if "capacity_percent" in charge:
            percent = charge["capacity_percent"]
            if type(percent) is not int or type(percent) is bool or not 0 <= percent <= 100:
                raise ValueError("reported charge capacity percent outside the declared range")
            row["dock_aircraft_charge_capacity_fraction"] = normalize(percent, "%")[1]
    sub = data.get("sub_device")
    if sub is not None:
        if not isinstance(sub, dict):
            raise ValueError("reported dock sub-device must be an object")
        if "device_sn" in sub:
            serial = sub["device_sn"]
            if not isinstance(serial, str) or not serial:
                raise ValueError("reported sub-device serial number must be nonempty text")
            row["dock_sub_device_sn_hex"] = "hex:" + serial.encode().hex()
        if "device_online_status" in sub:
            dock_enum(row, "dock_sub_device_online", "online_status", sub["device_online_status"])
        if "device_paired" in sub:
            dock_enum(row, "dock_sub_device_paired", "paired", sub["device_paired"])
    gateway = payload.get("gateway")
    if gateway is not None:
        if not isinstance(gateway, str) or not gateway:
            raise ValueError("reported gateway serial number must be nonempty text")
        row["dock_gateway_sn_hex"] = "hex:" + gateway.encode().hex()
    stamp = payload.get("timestamp")
    if stamp is not None:
        if type(stamp) is not int or type(stamp) is bool or not 0 <= stamp < 2**63:
            raise ValueError("reported payload millisecond timestamp must be a nonnegative integer")
        row["dock_payload_timestamp_ms_reported"] = stamp
    row.update(dock_payload_clock_basis=DOCK_CLOCK_BASIS,
               dock_source_declaration=DOCK_SOURCE_DECLARATION,
               dock_identity_basis=DOCK_IDENTITY_BASIS,
               dock_enum_basis=DOCK_ENUM_BASIS, dock_value_basis=DOCK_VALUE_BASIS)
    return row


def convert(text, topic, payload_format, *, vda_actions=False, vda_errors=False, envelope_format="j"):
    detailed = vda_actions or vda_errors
    if envelope_format not in ENVELOPE_FORMATS:
        raise ValueError("explicit supported mosquitto envelope format required")
    json_value_payload = envelope_format == "J"
    if (type(vda_actions) is not bool or type(vda_errors) is not bool
            or detailed and payload_format not in ("vda-state-2.1", "vda-state-3.0")):
        raise ValueError("detail selection requires VDA2.1 or3.0")
    if (payload_format not in FORMATS or not isinstance(topic, str) or not topic
            or any(c in topic for c in "#+\x00") or len(topic.encode()) > 65535):
        raise ValueError("explicit supported topic and payload format required")
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized capture")
    if payload_format in (DJI_FORMAT, DOCK_FORMAT) and not re.fullmatch(r"thing/product/[^/]+/osd", topic):
        # The dock publishes its own properties on the same OSD topic shape, with the gateway (station)
        # serial number in it, so the topic is required but never sufficient to choose the producer.
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
        if json_value_payload:
            # Only the selected object shape: a string is a %j line, and an arbitrary or binary
            # payload is not supported here. The reported length is carried, never checked against
            # this value, because the publisher may have reprinted it.
            if not isinstance(payload, dict):
                raise ValueError("selected %J envelope requires a JSON object payload")
            if type(record.get("payloadlen")) is not int or not 0 <= record["payloadlen"] <= LIMIT:
                raise ValueError("invalid reported payload byte count")
        elif (not isinstance(payload, str) or type(record.get("payloadlen")) is not int
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
        # Reformat only for the existing JSONL parser. Under %j the exact envelope retains the original
        # payload bytes; under %J it retains the publisher's own serialization of them. Duplicate keys
        # are refused either way - by this parse for %j, by the line parse itself for %J.
        payload_object = payload if json_value_payload else strict_json(payload)
        if payload_format == DOCK_FORMAT:
            # Built here rather than through the shared electrical converter: this is station status, not
            # an electrical schema, and it reuses the envelope, capture clock, digest and CSV path only.
            rows = [dock_row(payload_object)]
            envelope_digest = "sha256:" + hashlib.sha256(line.encode()).hexdigest()
            for row in rows:
                row.update(mqtt_source_record_index=source_index, mqtt_envelope_sha256=envelope_digest,
                           record_time_us=capture,
                           source_record_hex="hex:" + line.encode().hex(),
                           mqtt_topic_hex="hex:" + topic.encode().hex(),
                           mqtt_qos=record["qos"], mqtt_retain=record["retain"],
                           mqtt_clock_basis="CAPTURE_REPORTED")
                if json_value_payload:
                    row.update(mqtt_envelope_format=JSON_VALUE_ENVELOPE,
                               mqtt_payload_bytes_reported=record["payloadlen"],
                               mqtt_payload_length_basis=JSON_VALUE_LENGTH_BASIS)
                if writer is None:
                    writer = csv.DictWriter(output, fieldnames=list(row), lineterminator="\n")
                    writer.writeheader()
                writer.writerow(row)
                if output.tell() > LIMIT:
                    raise ValueError("converted capture exceeds bound")
            continue
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
            if json_value_payload:
                row.update(mqtt_envelope_format=JSON_VALUE_ENVELOPE,
                           mqtt_payload_bytes_reported=record["payloadlen"],
                           mqtt_payload_length_basis=JSON_VALUE_LENGTH_BASIS)
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
    parser.add_argument("--envelope-format", choices=ENVELOPE_FORMATS, default="j",
                        help="mosquitto_sub output specifier of the saved capture: %%j quoted payload "
                             "string (default), %%J payload as a JSON value whose byte length is "
                             "reported but cannot be verified")
    args = parser.parse_args()
    try:
        result = convert(args.input.read_text(encoding="utf-8"), args.topic, args.payload_format,
                         vda_actions=args.vda_actions, vda_errors=args.vda_errors,
                         envelope_format=args.envelope_format)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(result)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        parser.exit(2, "Saved MQTT conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
