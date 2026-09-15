"""Saved OPC UA 1.05.07 compact scalar DataValues, never a live UA client."""
import argparse
import base64
import csv
import io
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from scripts.convert_mqtt_recorded import strict_json
from scripts.convert_ardupilot_battery_csv import json_time, quantity
from scripts.recorded_units import UNITS, normalize
from scripts.convert_gpx_recorded import utc_microseconds, decimal_value
AEMP_FORMATS = ("iso-hours-xml-20190501", "takeuchi-v3-hours-json")
BASE = ["record_time_us", "node_id_hex", "source_record_hex", "status_code",
        "status_severity", "value_disposition", "source_time_us", "server_time_us",
        "source_timestamp_hex", "server_timestamp_hex", "source_10ps", "server_10ps"]
INTEGER_RANGES = {2: (-128, 127), 3: (0, 255), 4: (-32768, 32767),
                  5: (0, 65535), 6: (-2**31, 2**31 - 1), 7: (0, 2**32 - 1),
                  8: (-2**63, 2**63 - 1), 9: (0, 2**64 - 1)}
QUALITY_FIELDS = ["structure_changed_reported", "semantics_changed_reported", "metadata_refresh_required",
                  "status_info_type", "status_subcode", "status_reserved_bits", "limit_reported",
                  "queue_overflow_reported", "historian_origin_reported", "historian_partial_reported",
                  "historian_extra_data_reported", "historian_multi_value_reported"]


def quality_fields(code):
    info = (code >> 10) & 3
    result = dict.fromkeys(QUALITY_FIELDS)
    result.update(structure_changed_reported=int(bool(code & 0x8000)),
                  semantics_changed_reported=int(bool(code & 0x4000)),
                  metadata_refresh_required=int(bool(code & 0xc000)), status_subcode=(code >> 16) & 0xfff,
                  status_reserved_bits=code & 0x30003000,
                  status_info_type=("NOT_USED", "DATA_VALUE", "RESERVED", "RESERVED")[info])
    if info == 1:
        result.update(limit_reported=("NONE", "LOW", "HIGH", "CONSTANT")[(code >> 8) & 3],
                      queue_overflow_reported=int(bool(code & 0x80)),
                      historian_origin_reported=("RAW", "CALCULATED", "INTERPOLATED", "RESERVED")[code & 3],
                      historian_partial_reported=int(bool(code & 4)),
                      historian_extra_data_reported=int(bool(code & 8)),
                      historian_multi_value_reported=int(bool(code & 16)))
        result["status_reserved_bits"] |= code & 0x60
    return result


def numeric_value(data):
    kind, value = data["UaType"], data.get("Value")
    if kind in INTEGER_RANGES:
        if kind in (8, 9):
            if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]{1,20}", value):
                raise ValueError("64-bit UA integer requires decimal JSON string")
            value = int(value)
        elif type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)) or value != int(value):
            raise ValueError("UA integer requires integral JSON number")
        value = int(value)
        low, high = INTEGER_RANGES[kind]
        if not low <= value <= high:
            raise ValueError("UA integer outside declared type range")
        if value > 2**63 - 1:
            raise ValueError("UA UInt64 exceeds common signed integer range")
        return value
    value = quantity(value, "OPC UA floating-point")
    if kind == 10 and abs(value) > 3.4028234663852886e38:
        raise ValueError("UA Float exceeds finite float32 range")
    return value


def convert(text, node, unit=None, *, boolean=False):
    if type(boolean) is not bool or (boolean and unit is not None):
        raise ValueError("Boolean selection cannot carry an engineering unit")
    if not isinstance(node, str) or not node or len(node.encode()) > 1024 or (not boolean and unit not in UNITS):
        raise ValueError("explicit qualified node and unit required")
    if not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("empty or oversized input")
    output = io.StringIO(newline="")
    old_fields = ["voltage_v", "current_a", "temperature_k", "distance_m", "pressure_pa"]
    fields = old_fields + sorted({v[0] for v in UNITS.values()} - set(old_fields))
    if boolean:
        fields = ["boolean_reported"]
    writer = csv.DictWriter(output, BASE + fields + ["selected_unit"] + QUALITY_FIELDS, lineterminator="\n")
    writer.writeheader()
    report = dict(records=0, converted_values=0, withheld_values=0, clock="Unknown")
    previous = None
    for line in text.splitlines():
        if not line.strip() or len(line.encode()) > 12000:
            raise ValueError("empty or oversized record")
        record = strict_json(line)
        if not isinstance(record, dict) or record.get("node_id") != node:
            raise ValueError("source node mismatch")
        capture = json_time(record, "farmbot-state")
        if previous is not None and capture < previous:
            raise ValueError("decreasing capture time")
        previous = capture
        data = record.get("data_value")
        if (not isinstance(data, dict) or type(data.get("UaType")) is not int
                or not (data["UaType"] == 1 if boolean else 2 <= data["UaType"] <= 11)):
            raise ValueError("explicit selected compact scalar UaType required")
        if "Dimensions" in data or isinstance(data.get("Value"), (list, dict)):
            raise ValueError("scalar DataValue required")
        status = data.get("Status", {})
        if not isinstance(status, dict) or "Symbol" in status:
            raise ValueError("compact StatusCode object required")
        code = status.get("Code", 0)
        if type(code) is not int or not 0 <= code <= 2**32 - 1:
            raise ValueError("invalid StatusCode")
        severity = ("GOOD_REPORTED", "UNCERTAIN_REPORTED", "BAD_REPORTED", "RESERVED_AS_BAD")[code >> 30]
        row = dict(record_time_us=capture, node_id_hex="hex:" + node.encode().hex(),
                   source_record_hex="hex:" + line.encode().hex(), status_code=code,
                   status_severity=severity, selected_unit=unit)
        row.update(quality_fields(code))
        for prefix, key in (("source", "Source"), ("server", "Server")):
            stamp = data.get(key + "Timestamp")
            pico = data.get(key + "Picoseconds", 0)
            if type(pico) is not int or not 0 <= pico <= 65535:
                raise ValueError("invalid UInt16 picoseconds")
            row[prefix + "_10ps"] = pico
            if stamp is not None and stamp != "0001-01-01T00:00:00Z":
                row[prefix + "_time_us"] = json_time({"timestamp": stamp}, "utc-z")
                row[prefix + "_timestamp_hex"] = "hex:" + stamp.encode().hex()
            elif pico:
                raise ValueError("picoseconds without timestamp")
        # Only exact Good is used numerically: other info/subcodes require additional interpretation.
        if code == 0:
            if boolean:
                if type(data.get("Value")) is not bool:
                    raise ValueError("explicit JSON Boolean required")
                row["boolean_reported"] = "TRUE" if data["Value"] else "FALSE"
                row["value_disposition"] = "BOOLEAN_REPORTED"
            else:
                value = numeric_value(data)
                field, normalized = normalize(value, unit)
                row[field] = normalized
                row["value_disposition"] = "NORMALIZED_REPORTED"
            report["converted_values"] += 1
        else:
            row["value_disposition"] = "WITHHELD_STATUS_NOT_EXACT_GOOD"
            report["withheld_values"] += 1
        writer.writerow(row)
        report["records"] += 1
        if output.tell() > 16 * 1024 * 1024:
            raise ValueError("expanded output exceeds bound")
    return output.getvalue(), report


def convert_sensorthings(text, property_definition, capture_time_us):
    """Saved expanded 1.1 scalar collection, not an API client or conformance claim."""
    if not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("bounded saved collection required")
    if not isinstance(property_definition, str) or not property_definition:
        raise ValueError("explicit observed property definition required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    source = strict_json(text)
    if not isinstance(source, dict) or not isinstance(source.get("value"), list) or not source["value"]:
        raise ValueError("nonempty Observation collection required")
    next_link = source.get("@iot.nextLink")
    if "@iot.nextLink" in source and (not isinstance(next_link, str) or not next_link):
        raise ValueError("invalid nextLink")
    output = io.StringIO(newline="")
    fields = ["record_time_us", "source_record_hex", "source_sha256", "source_index",
              "observation_id_hex", "datastream_id_hex", "observed_property_hex",
              "temperature_k", "value_disposition", "phenomenon_time_us", "result_time_us",
              "phenomenon_time_kind", "phenomenon_start_us", "phenomenon_end_us"]
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    report = dict(records=0, converted_values=0, withheld_values=0, source_sha256=digest,
                  collection_scope="PAGE_ONLY_NEXT_LINK_PRESENT" if next_link else "SUPPLIED_COLLECTION_ONLY",
                  next_link=next_link, clock="Unknown", quality="RETAINED_NOT_INTERPRETED")
    ids = set()
    for index, observation in enumerate(source["value"]):
        if not isinstance(observation, dict):
            raise ValueError("Observation object required")
        stream = observation.get("Datastream")
        if not isinstance(stream, dict) or stream.get("observationType") != "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement":
            raise ValueError("expanded measurement Datastream required")
        unit = stream.get("unitOfMeasurement")
        expected = {"name": "degree Celsius", "symbol": "°C", "definition": "http://unitsofmeasure.org/ucum.html#para-30"}
        observed = stream.get("ObservedProperty")
        if unit != expected or not isinstance(observed, dict) or observed.get("definition") != property_definition:
            raise ValueError("unqualified unit or observed property")
        row = dict(record_time_us=capture_time_us, source_sha256=digest, source_index=index,
                   source_record_hex="hex:" + text.encode().hex() if index == 0 else "",
                   observed_property_hex="hex:" + property_definition.encode().hex())
        for entity, field in ((observation, "observation_id_hex"), (stream, "datastream_id_hex")):
            value = entity.get("@iot.id")
            if type(value) not in (int, str) or isinstance(value, str) and not value:
                raise ValueError("explicit entity id required")
            encoded = json.dumps(value, ensure_ascii=False)
            row[field] = "hex:" + encoded.encode().hex()
        if row["observation_id_hex"] in ids:
            raise ValueError("duplicate Observation id")
        ids.add(row["observation_id_hex"])
        for key, field in (("phenomenonTime", "phenomenon_time_us"), ("resultTime", "result_time_us")):
            if key not in observation or key == "phenomenonTime" and observation[key] is None:
                raise ValueError("required observation time missing")
            if observation[key] is not None and not isinstance(observation[key], str):
                raise ValueError("reported time must be text or nullable resultTime")
            value = observation[key]
            if key == "phenomenonTime" and "/" in value:
                endpoints = value.split("/")
                if len(endpoints) != 2:
                    raise ValueError("explicit start/end phenomenon period required")
                start, end = map(utc_microseconds, endpoints)
                if end < start:
                    raise ValueError("reversed phenomenon period")
                row.update(phenomenon_time_kind="PERIOD", phenomenon_start_us=start,
                           phenomenon_end_us=end)
            else:
                row[field] = None if value is None else utc_microseconds(value)
                if key == "phenomenonTime":
                    row["phenomenon_time_kind"] = "INSTANT"
        if "result" not in observation:
            raise ValueError("result missing")
        value = observation["result"]
        row["value_disposition"] = "NULL_NOT_PROVIDED" if value is None else "REPORTED_QUALITY_UNASSESSED"
        if value is None:
            report["withheld_values"] += 1
        else:
            row["temperature_k"] = normalize(value, "degC")[1]
            report["converted_values"] += 1
        writer.writerow(row)
        report["records"] += 1
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("expanded output exceeds bound")
    return output.getvalue(), report


def convert_har_sensorthings(text, entry_index, property_definition):
    """Selected saved HAR response only; never execute a URL or expose headers."""
    if not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("bounded HAR input required")
    source = strict_json(text.removeprefix("\ufeff"))
    log = source["log"]
    if log["version"] != "1.2" or not isinstance(log["entries"], list):
        raise ValueError("HAR1.2 entries required")
    if type(entry_index) is not int or not 0 <= entry_index < len(log["entries"]):
        raise ValueError("explicit existing HAR entry index required")
    entry = log["entries"][entry_index]
    response = entry["response"]
    if entry["request"]["method"] != "GET" or type(response["status"]) is not int or response["status"] != 200:
        raise ValueError("qualified saved GET200 response required")
    content = response["content"]
    mime = content["mimeType"]
    if not isinstance(mime, str) or not re.fullmatch(r"application/(?:json|[A-Za-z0-9.!#$&^_+-]+\+json)(?:\s*;.*)?", mime):
        raise ValueError("JSON response content required")
    body = content["text"]
    if not isinstance(body, str):
        raise ValueError("saved body text required")
    encoding = content.get("encoding")
    if encoding is not None:
        if encoding != "base64":
            raise ValueError("unsupported HAR body encoding")
        body = base64.b64decode(body, validate=True).decode("utf-8")
    # HAR text is already decompressed/unchunked and transcoded by the exporter.
    # content.size can describe pre-transcoding bytes; never infer truncation from
    # equality with UTF8 length or decompress it a second time.
    if type(content["size"]) is not int or content["size"] < 0:
        raise ValueError("reported content size required")
    capture = utc_microseconds(entry["startedDateTime"])
    converted, report = convert_sensorthings(body, property_definition, capture)
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    previous_limit = csv.field_size_limit(34 * 1024 * 1024)
    try:
        rows = csv.DictReader(io.StringIO(converted))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, [*rows.fieldnames, "har_source_sha256", "har_entry_index", "har_clock_basis"], lineterminator="\n")
        writer.writeheader()
        for row in rows:
            row.update(har_source_sha256=digest, har_entry_index=entry_index,
                       har_clock_basis="REPORTED_REQUEST_START_NOT_MEASUREMENT_OR_RESPONSE_END")
            writer.writerow(row)
            if output.tell() > 128 * 1024 * 1024:
                raise ValueError("expanded HAR output exceeds bound")
    finally:
        csv.field_size_limit(previous_limit)
    report.update(har_source_sha256=digest, har_entry_index=entry_index,
                  har_entries=len(log["entries"]), har_unselected_entries=len(log["entries"]) - 1,
                  har_content_size_reported=content["size"], decoded_utf8_bytes=len(body.encode()),
                  har_source_retention="original HAR required; headers/cookies not copied; selected body retained",
                  har_clock_basis="REQUEST_START_NOT_MEASUREMENT_OR_RESPONSE_END")
    return output.getvalue(), report


def convert_aemp_hours(text, equipment_id, capture_time_us, layout):
    """Selected cumulative engine-hour page; not work performed or meter continuity."""
    if layout not in AEMP_FORMATS or not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("bounded selected operating-hours layout required")
    if not isinstance(equipment_id, str) or not equipment_id or len(equipment_id.encode()) > 4096:
        raise ValueError("caller-qualified equipment identifier required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    unsupported = 0
    if layout == "takeuchi-v3-hours-json":
        source = strict_json(text)
        records, links = source["CumulativeOperatingHours"], source["Links"]
    else:
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
            raise ValueError("XML declarations/entities not supported")
        ns = "{http://www.jcmanet.or.jp/english2017/ISO/15143/-3/20190501}"
        root = ET.fromstring(text)
        if root.tag != ns + "CumulativeOperatingHoursMessages":
            raise ValueError("wrong operating-hours XML namespace/root")
        records, links = [], []
        for node in root:
            if node.tag == ns + "CumulativeOperatingHours":
                hours = node.findall(ns + "Hour")
                if len(hours) != 1 or node.find(ns + "datetime") is not None:
                    raise ValueError("standard XML needs one Hour and datetime attribute")
                records.append(dict(Hour=decimal_value(hours[0].text, low=0), datetime=node.attrib["datetime"]))
            elif node.tag == ns + "Links":
                rel, href = node.findall(ns + "rel"), node.findall(ns + "href")
                if len(rel) != 1 or len(href) != 1:
                    raise ValueError("invalid saved link")
                links.append(dict(rel=rel[0].text, href=href[0].text))
            else:
                unsupported += 1
    if not isinstance(records, list) or not records or not isinstance(links, list) or not links:
        raise ValueError("nonempty operating-hours page and declared links required")
    for link in links:
        if not isinstance(link, dict) or any(not isinstance(link.get(k), str) or not link[k] for k in ("rel", "href")):
            raise ValueError("invalid saved link declaration")
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_record_hex", "source_sha256", "source_index",
        "equipment_id_hex", "engine_operating_seconds_reported", "operating_hours_measurement_time_us", "operating_hours_basis"])
    for index, record in enumerate(records):
        hours = quantity(record["Hour"], "engine hours")
        seconds = hours * 3600
        if hours < 0 or not math.isfinite(seconds):
            raise ValueError("invalid cumulative operating hours")
        stamp = utc_microseconds(record["datetime"])
        writer.writerow([capture_time_us, "hex:" + text.encode().hex() if index == 0 else "", digest, index,
            "hex:" + equipment_id.encode().hex(), seconds, stamp, "REPORTED_LIFETIME_ENGINE_TIME_NOT_WORK_OR_CONTINUITY"])
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("operating-hours output exceeds bound")
    return output.getvalue(), dict(records=len(records), source_sha256=digest, source_format=layout,
        links_retained_not_followed=links, collection_scope="SUPPLIED_PAGE_ONLY",
        unsupported_root_elements=unsupported, clock="Unknown", equipment_identity="CALLER_DECLARED_NOT_AUTHENTICATED")


def convert_onvif_status(text, profile_id, capture_time_us):
    """Saved status only; generic coordinates are not angles or magnification."""
    if not text or len(text.encode()) > 1024 * 1024 or "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("bounded XML without entity declarations required")
    if not isinstance(profile_id, str) or not profile_id or len(profile_id.encode()) > 4096:
        raise ValueError("caller-declared profile identifier required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture clock required")
    ns, ptz, soap = "{http://www.onvif.org/ver10/schema}", "{http://www.onvif.org/ver20/ptz/wsdl}", "{http://www.w3.org/2003/05/soap-envelope}"
    def one(parent, tag, required=False):
        found = parent.findall(tag)
        if len(found) > 1 or (required and not found):
            raise ValueError("missing or duplicated selected element")
        return found[0] if found else None
    try:
        root = ET.fromstring(text)
        if root.tag == soap + "Envelope":
            body = one(root, soap + "Body", True)
            if len(body) != 1:
                raise ValueError("one saved SOAP response required")
            root = body[0]
        if root.tag != ptz + "GetStatusResponse":
            raise ValueError("not a saved PTZ status response")
        status = one(root, ptz + "PTZStatus", True)
        stamp = one(status, ns + "UtcTime", True)
        if len(stamp):
            raise ValueError("scalar reported UTC time required")
        row = dict(record_time_us=capture_time_us, profile_id_hex="hex:" + profile_id.encode().hex(),
                   status_time_us=utc_microseconds(stamp.text), status_time_text_hex="hex:" + stamp.text.encode().hex(),
                   source_response_sha256="sha256:" + hashlib.sha256(text.encode()).hexdigest())
        position = one(status, ns + "Position")
        interpreted = 0
        if position is not None:
            for name, axes, low, high, prefix in (("PanTilt", ("x", "y"), -1, 1, "pan_tilt"),
                                                 ("Zoom", ("x",), 0, 1, "zoom")):
                vector = one(position, ns + name)
                if vector is None:
                    continue
                space = "http://www.onvif.org/ver10/tptz/" + name + "Spaces/PositionGenericSpace"
                if len(vector) or vector.get("space") != space:
                    raise ValueError("explicit selected generic coordinate space required")
                for axis in axes:
                    row[prefix + "_" + axis + "_normalized"] = float(decimal_value(vector.attrib[axis], low=low, high=high))
                    interpreted += 1
        move = one(status, ns + "MoveStatus")
        if move is not None:
            for name, field in (("PanTilt", "pan_tilt_move_reported"), ("Zoom", "zoom_move_reported")):
                value = one(move, ns + name)
                if value is not None:
                    if len(value) or value.text not in ("IDLE", "MOVING", "UNKNOWN"):
                        raise ValueError("unqualified move status")
                    row[field] = value.text
        error = one(status, ns + "Error")
        if error is None and any(row.get(key) == "UNKNOWN" for key in ("pan_tilt_move_reported", "zoom_move_reported")):
            raise ValueError("unknown movement status requires reported error")
        if error is not None:
            if len(error):
                raise ValueError("scalar error text required")
            row["error_text_hex"] = "hex:" + (error.text or "").encode().hex()
        row["position_disposition"] = "REPORTED_GENERIC_COORDINATES" if interpreted else "POSITION_NOT_REPORTED"
    except (ET.ParseError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("malformed saved PTZ response") from error
    fields = ["record_time_us", "profile_id_hex", "status_time_us", "status_time_text_hex", "source_response_sha256",
              "pan_tilt_x_normalized", "pan_tilt_y_normalized", "zoom_x_normalized", "position_disposition",
              "pan_tilt_move_reported", "zoom_move_reported", "error_text_hex"]
    output = io.StringIO(newline=""); writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader(); writer.writerow(row)
    return output.getvalue(), dict(observations=1, interpreted_coordinates=interpreted, clock="Unknown",
        uninterpreted_status_elements=sum(child.tag not in {ns + name for name in ("Position", "MoveStatus", "Error", "UtcTime")} for child in status),
        source_response=text, source_response_sha256=row["source_response_sha256"],
        scope="saved response, caller profile not authenticated; source extensions retained not interpreted")


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--node")
    parser.add_argument("--unit", choices=UNITS)
    parser.add_argument("--boolean", action="store_true", help="explicit UaType1 report without a physical unit or inferred device state")
    parser.add_argument("--source-format", choices=("opcua", "sensorthings-1.1-celsius", "har-1.2-sensorthings", "onvif-ptz-status-1812", *AEMP_FORMATS), default="opcua")
    parser.add_argument("--profile-id")
    parser.add_argument("--equipment-id")
    parser.add_argument("--har-entry", type=int)
    parser.add_argument("--observed-property")
    parser.add_argument("--capture-time-us", type=int)
    args = parser.parse_args()
    if args.source_format != "onvif-ptz-status-1812" and args.profile_id is not None:
        raise ValueError("profile ID requires explicit saved PTZ format")
    if args.source_format not in AEMP_FORMATS and args.equipment_id is not None:
        raise ValueError("equipment ID requires explicit operating-hours format")
    if args.source_format != "har-1.2-sensorthings" and args.har_entry is not None:
        raise ValueError("HAR entry requires explicit HAR format")
    with args.input.open("rb") as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("input exceeds bound")
    if args.source_format == "onvif-ptz-status-1812":
        if args.node is not None or args.unit is not None or args.boolean or args.observed_property is not None:
            raise ValueError("unrelated options do not qualify PTZ status")
        output, report = convert_onvif_status(raw.decode("utf-8"), args.profile_id, args.capture_time_us)
    elif args.source_format in AEMP_FORMATS:
        if args.node is not None or args.unit is not None or args.boolean or args.observed_property is not None:
            raise ValueError("unrelated options do not qualify operating hours")
        output, report = convert_aemp_hours(raw.decode("utf-8"), args.equipment_id, args.capture_time_us, args.source_format)
    elif args.source_format == "opcua":
        if args.observed_property is not None or args.capture_time_us is not None:
            raise ValueError("SensorThings options require explicit format")
        output, report = convert(raw.decode("utf-8"), args.node, args.unit, boolean=args.boolean)
    else:
        if args.node is not None or args.unit is not None or args.boolean:
            raise ValueError("OPC options do not qualify SensorThings units")
        if args.source_format == "har-1.2-sensorthings":
            if args.capture_time_us is not None:
                raise ValueError("HAR request clock must not be overridden")
            output, report = convert_har_sensorthings(raw.decode("utf-8"), args.har_entry, args.observed_property)
        else:
            output, report = convert_sensorthings(raw.decode("utf-8"), args.observed_property, args.capture_time_us)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(output)
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
