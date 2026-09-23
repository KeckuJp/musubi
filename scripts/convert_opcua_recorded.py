"""Saved OPC UA 1.05.07 compact scalar or 1-D Double array DataValues, never a live UA client."""
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
FLEET_HOURS_FORMAT = "iso-fleet-hours-xml-20190501"
AEMP_FORMATS = ("iso-hours-xml-20190501", FLEET_HOURS_FORMAT, "takeuchi-v3-hours-json")
# ISO 15143-3 20190501 common.xsd (sha256 03669def0875f75057bbe96162cdcfd534f383cb7433ba8c302b6efaa4059d0c)
# declares one global element, Fleet, whose Equipment records carry an EquipmentHeader and at most one
# CumulativeOperatingHours of the same type the attribute-form container already uses. These are the
# element names that schema defines; they are not validated against the XSD at conversion time.
FLEET_HEADER_FIELDS = ("UnitInstallDateTime", "OEMName", "Model", "EquipmentID", "SerialNumber", "PIN")
FLEET_METRICS = ("AverageLoadFactorLast24", "Location", "CumulativeActiveRegenerationHours",
                 "CumulativeIdleHours", "CumulativeIdleNonOperatingHours", "CumulativeLoadCount",
                 "CumulativePowerTakeOffHours", "CumulativePayloadTotals", "DEFRemaining", "Distance",
                 "EngineStatus", "FuelUsed", "FuelUsedLast24", "FuelRemaining", "MaximumSpeedLast24")
FLEET_FIELDS = ["equipment_report_snapshot_time_us", "fleet_version_reported", "equipment_index",
                "equipment_id_reported_hex", "equipment_serial_reported_hex", "equipment_pin_reported_hex",
                "equipment_oem_reported_hex", "equipment_model_reported_hex", "equipment_hours_disposition"]
MAX_FLEET_EQUIPMENT = 20000
BASE = ["record_time_us", "node_id_hex", "source_record_hex", "status_code",
        "status_severity", "value_disposition", "source_time_us", "server_time_us",
        "source_timestamp_hex", "server_timestamp_hex", "source_10ps", "server_10ps"]
INTEGER_RANGES = {2: (-128, 127), 3: (0, 255), 4: (-32768, 32767),
                  5: (0, 65535), 6: (-2**31, 2**31 - 1), 7: (0, 2**32 - 1),
                  8: (-2**63, 2**63 - 1), 9: (0, 2**64 - 1)}
# Part 6 1.05.07 5.4.5: a 1-D array is a JSON array. The elements are expanded into one row each, so the
# count is bounded before anything is materialised; the 12000-byte record bound alone would not say so.
ARRAY_FIELDS = ["opcua_array_ordinal", "opcua_array_length", "opcua_element_index",
                "opcua_array_disposition"]
MAX_ARRAY_ELEMENTS = 512
ARRAY_ELEMENT = "ORDERED_ELEMENT_OF_ONE_REPORTED_ARRAY"
ARRAY_EMPTY = "EMPTY_ARRAY_NO_ELEMENT_REPORTED"
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


def convert(text, node, unit=None, *, boolean=False, arrays=False):
    if type(boolean) is not bool or (boolean and unit is not None):
        raise ValueError("Boolean selection cannot carry an engineering unit")
    if type(arrays) is not bool or (arrays and boolean):
        raise ValueError("array selection applies to the numeric path only")
    if not isinstance(node, str) or not node or len(node.encode()) > 1024 or (not boolean and unit not in UNITS):
        raise ValueError("explicit qualified node and unit required")
    if not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("empty or oversized input")
    output = io.StringIO(newline="")
    old_fields = ["voltage_v", "current_a", "temperature_k", "distance_m", "pressure_pa"]
    fields = old_fields + sorted({v[0] for v in UNITS.values()} - set(old_fields))
    if boolean:
        fields = ["boolean_reported"]
    writer = csv.DictWriter(output, BASE + fields + ["selected_unit"] + QUALITY_FIELDS
                            + (ARRAY_FIELDS if arrays else []), lineterminator="\n")
    writer.writeheader()
    report = dict(records=0, converted_values=0, withheld_values=0, clock="Unknown")
    if arrays:
        report.update(arrays=0, array_elements=0, empty_arrays=0,
                      element_order="SOURCE_JSON_ORDER_NOT_A_COMPONENT_OR_AXIS_ASSIGNMENT")
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
                or not (data["UaType"] == 1 if boolean else
                        data["UaType"] == 11 if arrays else 2 <= data["UaType"] <= 11)):
            raise ValueError("explicit selected compact scalar UaType required" if not arrays
                             else "explicit selected UaType 11 array required")
        if arrays:
            elements = data.get("Value")
            if not isinstance(elements, list):
                raise ValueError("selected array DataValue required")
            # The inspected pin defines Dimensions only for multidimensional arrays and says nothing
            # about it on a 1-D array, so this selection admits the one shape the text does define: a
            # plain JSON array with no Dimensions field. Any Dimensions presence is refused rather
            # than matched against a length rule no section here states - which would also have let a
            # JSON true or 1.0 stand in for the length, since Python compares them equal to 1.
            if "Dimensions" in data:
                raise ValueError("unsupported array dimensions")
            if len(elements) > MAX_ARRAY_ELEMENTS:
                raise ValueError("array exceeds the expanded element bound")
            # Structure is checked for every element before the quality branch: an unsupported shape
            # is refused whatever the StatusCode says, so a Bad array cannot smuggle one through.
            if any(type(element) not in (int, float) or isinstance(element, bool)
                   for element in elements):
                # A JSON null, a special-value string, a Boolean or a nested array is not a defined
                # Double element here: Double is not nullable, so none of them becomes a zero.
                raise ValueError("array element is not a defined JSON number")
        elif "Dimensions" in data or isinstance(data.get("Value"), (list, dict)):
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
        if arrays:
            report["arrays"] += 1
            row.update(opcua_array_ordinal=report["arrays"], opcua_array_length=len(elements))
            if not elements:
                # An empty array is neither a value nor something to drop: it is accounted on its own row.
                row.update(opcua_array_disposition=ARRAY_EMPTY,
                           value_disposition="NO_ELEMENT_REPORTED_IN_THIS_ARRAY")
                report["empty_arrays"] += 1
                writer.writerow(row)
                report["records"] += 1
                if output.tell() > 16 * 1024 * 1024:
                    raise ValueError("expanded output exceeds bound")
                continue
            for index, element in enumerate(elements):
                # One row per element in source order; the status and both clocks belong to the whole
                # DataValue (Part 6 Table 42), so every row of one array repeats them.
                cell = dict(row, opcua_element_index=index,
                            opcua_array_disposition=ARRAY_ELEMENT)
                if code == 0:
                    # Only exact Good is interpreted numerically; a withheld element is never ranged,
                    # normalized or promoted.
                    field, normalized = normalize(numeric_value({"UaType": 11, "Value": element}), unit)
                    cell[field] = normalized
                    cell["value_disposition"] = "NORMALIZED_REPORTED"
                    report["converted_values"] += 1
                else:
                    cell["value_disposition"] = "WITHHELD_STATUS_NOT_EXACT_GOOD"
                    report["withheld_values"] += 1
                writer.writerow(cell)
                report["array_elements"] += 1
                if output.tell() > 16 * 1024 * 1024:
                    raise ValueError("expanded output exceeds bound")
            report["records"] += 1
            continue
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


def saved_page_gate(text, property_definition, capture_time_us, object_reason):
    """The checks a saved page must pass whether it is converted alone or inside a chain.

    A page whose Observation collection is empty emits no row, so it never reaches the per-observation
    semantics below. Without a shared gate such a page would bypass the body bound and the operator's
    own arguments entirely, so both callers run this first. Reasons are fixed text: a page body or a
    declared url may carry query secrets and is never echoed. Returns the parsed body.
    """
    if not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("bounded saved collection required")
    if not isinstance(property_definition, str) or not property_definition:
        raise ValueError("explicit observed property definition required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    source = strict_json(text)
    if not isinstance(source, dict):
        raise ValueError(object_reason)
    return source


def convert_sensorthings(text, property_definition, capture_time_us):
    """Saved expanded 1.1 scalar collection, not an API client or conformance claim."""
    # The adopted single-page reasons and their order are unchanged: a non-object body is still
    # refused as an absent nonempty collection here.
    source = saved_page_gate(text, property_definition, capture_time_us,
                             "nonempty Observation collection required")
    if not isinstance(source.get("value"), list) or not source["value"]:
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


SENSORTHINGS_PAGES_FORMAT = "sensorthings-1.1-celsius-pages"
PAGE_FIELDS = ["saved_page_index", "saved_page_count", "saved_page_sha256", "saved_page_url_reference",
               "saved_chain_disposition", "reported_collection_count", "observation_repeat_disposition"]
PAGE_URL_REDACTED = "REDACTED_URL_SEE_RETAINED_MANIFEST"
CHAIN_TERMINAL = "SUPPLIED_CHAIN_TERMINAL_PAGE_CARRIED_NO_NEXT_LINK"
CHAIN_UNFOLLOWED = "SUPPLIED_CHAIN_ENDS_WITH_UNFOLLOWED_NEXT_LINK"
MAX_SAVED_PAGES = 512
MAX_CHAIN_OBSERVATIONS = 200000


def sensorthings_saved_pages(text, property_definition, capture_time_us):
    """A caller-bound finite set of saved pages; never a client, never a completeness claim.

    Each manifest line carries the URL the caller says a page was saved from and that page's exact saved
    text. Page N's @iot.nextLink must equal page N+1's declared URL, so a missing or misordered page is
    refused rather than quietly completed. Nothing is fetched, no URL is resolved and no chain is ever
    called complete: the terminal disposition says only whether the last supplied body still carried a
    nextLink. OGC 18-088 makes the link opaque and is silent on cross-page consistency, so the spec
    supports nothing stronger.
    """
    if not text or len(text.encode()) > 64 * 1024 * 1024:
        raise ValueError("bounded saved page manifest required")
    pages, seen_urls = [], set()
    for line in text.splitlines():
        if not line.strip():
            continue
        entry = strict_json(line)
        if not isinstance(entry, dict):
            raise ValueError("saved page manifest line must be an object")
        url, body = entry.get("declared_page_url"), entry.get("page_body")
        if not isinstance(url, str) or not url or not isinstance(body, str) or not body:
            raise ValueError("each saved page needs a declared url and its exact saved body text")
        if url in seen_urls:
            # A repeated declared url would let a nextLink loop be ingested as progress.
            raise ValueError("repeated declared page url in the saved chain")
        seen_urls.add(url)
        # Every page passes the shared gate, and the chain-wide shape checks, before anything branches
        # on whether the page carries observations. An empty page emits no row, but it must not
        # thereby bypass the body bound, the operator arguments, the nextLink shape or the reported
        # count. A fixed reason: never echo the body or the url, which may carry query secrets.
        parsed = saved_page_gate(body, property_definition, capture_time_us,
                                 "saved page body must be a JSON object")
        link = parsed.get("@iot.nextLink")
        if "@iot.nextLink" in parsed and (not isinstance(link, str) or not link):
            # An explicit null is invalid exactly as the single-page gate treats it, never absence.
            raise ValueError("invalid nextLink")
        reported = parsed.get("@iot.count")
        if reported is not None and (type(reported) is not int or reported < 0):
            raise ValueError("invalid reported collection count")
        pages.append((url, body, parsed, {k: v for k, v in entry.items()
                                          if k not in ("declared_page_url", "page_body")}))
        if len(pages) > MAX_SAVED_PAGES:
            raise ValueError("saved chain exceeds the page bound")
    if not pages:
        raise ValueError("at least one saved page required")

    # The binding is a string comparison only: the specification makes the nextLink opaque.
    for index, (_, _, parsed, _) in enumerate(pages[:-1]):
        link = parsed.get("@iot.nextLink")
        if not isinstance(link, str) or link != pages[index + 1][0]:
            raise ValueError("saved chain break: nextLink does not match the next declared page url")
    # Shape already gated per page, so presence here is a nonempty string and absence is absence.
    disposition = CHAIN_UNFOLLOWED if pages[-1][2].get("@iot.nextLink") else CHAIN_TERMINAL

    output = io.StringIO(newline="")
    writer, ids, counted = None, {}, 0
    report = dict(records=0, converted_values=0, withheld_values=0, saved_pages=len(pages),
                  saved_chain_disposition=disposition, repeated_observation_ids=0,
                  empty_pages=0, empty_page_indices=[], empty_page_provenance=[],
                  retained_manifest_keys=[],
                  # The retained input is the only place the raw urls, bodies and extra key values
                  # live; this digest binds the exact manifest a report was produced from, so an
                  # empty page's extra values and raw source stay resolvable without copying them.
                  manifest_sha256="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                  clock="Unknown", quality="RETAINED_NOT_INTERPRETED",
                  chain_basis=("supplied saved pages only; the specification makes the nextLink opaque and "
                               "says nothing about cross-page consistency, so this is not a complete "
                               "collection, a snapshot, an authenticated body or a fetched chain"))
    for index, (url, body, parsed, extra) in enumerate(pages):
        # Retained key names, the page digest, the redacted url reference and the reported count are
        # established for every page, empty or not, before the branch below.
        for name in extra:
            if name not in report["retained_manifest_keys"]:
                report["retained_manifest_keys"].append(name)
        page_digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        url_reference = (PAGE_URL_REDACTED + "@sha256:" + hashlib.sha256(url.encode()).hexdigest()
                         + "+" + str(len(url.encode())))
        reported_count = parsed.get("@iot.count")
        if isinstance(parsed.get("value"), list) and not parsed["value"]:
            # A server may end (or interleave) a chain with an empty collection. It contributes no
            # observation, so no row is fabricated, but the page is counted rather than ignored, and
            # its provenance goes to the report because no row of its own can carry it.
            report["empty_pages"] += 1
            report["empty_page_indices"].append(index)
            report["empty_page_provenance"].append(dict(
                saved_page_index=index, saved_page_count=len(pages), saved_page_sha256=page_digest,
                saved_page_url_reference=url_reference, saved_chain_disposition=disposition,
                reported_collection_count="" if reported_count is None else reported_count,
                retained_manifest_keys=sorted(extra)))
            continue
        # The adopted single-page semantics are reused unchanged, per page.
        page_output, page_report = convert_sensorthings(body, property_definition, capture_time_us)
        # Row 0 of a page carries that page's whole body as hex, which can exceed the csv module's
        # default field limit on a large page. Raise it only for this parse and put it back, so no other
        # reader in the process inherits a widened limit.
        previous_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(64 * 1024 * 1024)
            rows = list(csv.DictReader(io.StringIO(page_output)))
        finally:
            csv.field_size_limit(previous_limit)
        counted += len(rows)
        if counted > MAX_CHAIN_OBSERVATIONS:
            raise ValueError("saved chain exceeds the observation bound")
        for row in rows:
            identity = row["observation_id_hex"]
            repeat = "FIRST_REPORT_OF_THIS_ID" if identity not in ids else (
                "ID_REPORTED_AGAIN_IN_THE_SUPPLIED_CHAIN_NEITHER_BODY_RANKED")
            if identity in ids:
                report["repeated_observation_ids"] += 1
            ids[identity] = True
            row.update(saved_page_index=index, saved_page_count=len(pages),
                       saved_page_sha256=page_report["source_sha256"],
                       saved_page_url_reference=url_reference,
                       saved_chain_disposition=disposition,
                       reported_collection_count="" if reported_count is None else reported_count,
                       observation_repeat_disposition=repeat)
            if writer is None:
                writer = csv.DictWriter(output, list(row), lineterminator="\n")
                writer.writeheader()
            writer.writerow(row)
            report["records"] += 1
        report["converted_values"] += page_report["converted_values"]
        report["withheld_values"] += page_report["withheld_values"]
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("expanded output exceeds bound")
    if not report["records"] and not report["empty_pages"]:
        raise ValueError("no selected observation in the saved chain")
    return output.getvalue(), report


def har_log(text):
    if not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("bounded HAR input required")
    source = strict_json(text.removeprefix("\ufeff"))
    log = source["log"]
    if log["version"] != "1.2" or not isinstance(log["entries"], list):
        raise ValueError("HAR1.2 entries required")
    return log


def convert_har_status(text):
    """Saved outcomes only; credentials/body are retained solely in the original."""
    log = har_log(text)
    if not log["entries"]:
        raise ValueError("nonempty HAR entries required")
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    decoded = []
    classes = {1: "INFORMATIONAL_REPORTED", 2: "SUCCESS_STATUS_REPORTED", 3: "REDIRECTION_REPORTED",
               4: "CLIENT_ERROR_STATUS_REPORTED", 5: "SERVER_ERROR_STATUS_REPORTED"}
    for index, entry in enumerate(log["entries"]):
        status = entry["response"]["status"]
        if type(status) is not int or not (status == 0 or 100 <= status <= 599):
            raise ValueError("invalid reported HTTP status")
        elapsed = quantity(entry["time"], "HAR elapsed milliseconds")
        if elapsed < 0:
            raise ValueError("negative HAR elapsed time")
        decoded.append([utc_microseconds(entry["startedDateTime"]), index, status,
            classes.get(status // 100, "UNQUALIFIED_NO_HTTP_STATUS"), elapsed / 1000, digest,
            "REQUEST_START_AND_REPORTED_ELAPSED_NOT_OUTAGE_OR_BUSINESS_SUCCESS"])
    decoded.sort(key=lambda row: (row[0], row[1]))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "har_entry_index", "http_status_reported", "http_status_class",
                     "request_elapsed_reported_s", "har_source_sha256", "http_outcome_basis"])
    for row in decoded:
        writer.writerow(row)
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("HAR outcome output exceeds bound")
    return output.getvalue(), dict(records=len(decoded), source_entries=len(decoded), source_sha256=digest,
        order_changed=any(index != row[1] for index, row in enumerate(decoded)),
        original_required="All original fields remain in private HAR; credentials and bodies are not copied",
        clock="Unknown")


def convert_har_sensorthings(text, entry_index, property_definition):
    """Selected saved HAR response only; never execute a URL or expose headers."""
    log = har_log(text)
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


def aemp_hour_record(node, ns):
    """One CumulativeOperatingHours record: the schema's single Hour child and datetime attribute.

    Shared by both selected containers so there is one rule, not two. Returns the record and how
    many other children it carried, which the caller accounts for; nothing is dropped silently.
    """
    hours = node.findall(ns + "Hour")
    if len(hours) != 1 or node.find(ns + "datetime") is not None:
        raise ValueError("standard XML needs one Hour and datetime attribute")
    if len(hours[0]):
        # xs:decimal is a simple type: a nested element means the reported value is not scalar, and
        # reading only the leading text would both invent a quantity and hide that subtree.
        raise ValueError("scalar reported Hour value required")
    return (dict(Hour=decimal_value(hours[0].text, low=0), datetime=node.attrib["datetime"]),
            sum(child.tag != ns + "Hour" for child in node))


def aemp_link(node, ns):
    """One Links record: the schema's single rel and href, both simple strings.

    Shared by both selected containers. A nested element inside either would be silently
    truncated by reading its leading text, so it is refused; unknown siblings are counted
    and returned to the caller instead of being dropped.
    """
    rel, href = node.findall(ns + "rel"), node.findall(ns + "href")
    if len(rel) != 1 or len(href) != 1:
        raise ValueError("invalid saved link")
    if len(rel[0]) or len(href[0]):
        raise ValueError("scalar reported link rel and href required")
    return (dict(rel=rel[0].text, href=href[0].text),
            sum(child.tag not in (ns + "rel", ns + "href") for child in node))


def aemp_fleet_records(root, ns):
    """Selected Fleet snapshot equipment records; reported provenance, never proven identity.

    common.xsd defines Fleet with required version and snapshotTime attributes, one or more Links,
    and one or more Equipment, each with one EquipmentHeader and at most one
    CumulativeOperatingHours. An equipment that reports no hour record yields an accounted row
    without a value: absence is not zero and is not a missing reading to be filled in.
    """
    if root.tag != ns + "Fleet":
        raise ValueError("wrong operating-hours XML namespace/root")
    if any(name not in root.attrib for name in ("version", "snapshotTime")):
        raise ValueError("selected fleet snapshot needs version and snapshotTime attributes")
    version = root.attrib["version"]
    if not re.fullmatch(r"[0-9]{1,10}", version) or int(version) >= 2**32:
        raise ValueError("reported fleet version must be an unsigned integer")
    fleet = dict(snapshot_time_us=utc_microseconds(root.attrib["snapshotTime"]), version=int(version),
                 unselected_metric_elements=0, unknown_elements=0, absent_hour_records=0,
                 unsupported_root_elements=0)
    records, links = [], []
    for node in root:
        if node.tag == ns + "Links":
            link, extra = aemp_link(node, ns)
            links.append(link)
            fleet["unknown_elements"] += extra
        elif node.tag == ns + "Equipment":
            if len(records) >= MAX_FLEET_EQUIPMENT:
                # Refuse the volume; never convert a prefix and call the rest absent.
                raise ValueError("saved fleet exceeds the selected equipment bound")
            headers = node.findall(ns + "EquipmentHeader")
            if len(headers) != 1:
                raise ValueError("one reported EquipmentHeader per selected equipment required")
            record = dict(equipment_index=len(records))
            for field in FLEET_HEADER_FIELDS:
                found = headers[0].findall(ns + field)
                if len(found) > 1:
                    raise ValueError("duplicate reported equipment header field")
                if not found:
                    continue
                if len(found[0]):
                    raise ValueError("scalar reported equipment header field required")
                value = found[0].text or ""
                if field in ("SerialNumber", "PIN") and not 1 <= len(value) <= 17:
                    raise ValueError("reported serial number outside the selected schema length")
                if field == "UnitInstallDateTime":
                    utc_microseconds(value)  # checked against the same time rule; not a capture clock
                record[field] = value
            fleet["unknown_elements"] += sum(child.tag not in {ns + f for f in FLEET_HEADER_FIELDS}
                                             for child in headers[0])
            hours = node.findall(ns + "CumulativeOperatingHours")
            if len(hours) > 1:
                raise ValueError("ambiguous duplicate operating-hours record")
            if hours:
                if "datetime" not in hours[0].attrib:
                    raise ValueError("standard XML needs one Hour and datetime attribute")
                reported, extra = aemp_hour_record(hours[0], ns)
                record.update(reported)
                fleet["unknown_elements"] += extra
            else:
                fleet["absent_hour_records"] += 1
            for child in node:
                if child.tag in (ns + "EquipmentHeader", ns + "CumulativeOperatingHours"):
                    continue
                key = "unselected_metric_elements" if child.tag in {ns + m for m in FLEET_METRICS} else "unknown_elements"
                fleet[key] += 1
            records.append(record)
        else:
            fleet["unsupported_root_elements"] += 1
    return records, links, fleet


def convert_aemp_hours(text, equipment_id, capture_time_us, layout):
    """Selected cumulative engine-hour page; not work performed or meter continuity."""
    if layout not in AEMP_FORMATS or not text or len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("bounded selected operating-hours layout required")
    if not isinstance(equipment_id, str) or not equipment_id or len(equipment_id.encode()) > 4096:
        raise ValueError("caller-qualified equipment identifier required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    unsupported, fleet = 0, None
    if layout == "takeuchi-v3-hours-json":
        source = strict_json(text)
        records, links = source["CumulativeOperatingHours"], source["Links"]
    else:
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
            raise ValueError("XML declarations/entities not supported")
        ns = "{http://www.jcmanet.or.jp/english2017/ISO/15143/-3/20190501}"
        root = ET.fromstring(text)
        if layout == FLEET_HOURS_FORMAT:
            # The same namespace and the same hour record, nested per equipment instead of per page.
            records, links, fleet = aemp_fleet_records(root, ns)
        else:
            if root.tag != ns + "CumulativeOperatingHoursMessages":
                raise ValueError("wrong operating-hours XML namespace/root")
            records, links = [], []
            for node in root:
                if node.tag == ns + "CumulativeOperatingHours":
                    records.append(aemp_hour_record(node, ns)[0])
                elif node.tag == ns + "Links":
                    links.append(aemp_link(node, ns)[0])
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
        "equipment_id_hex", "engine_operating_seconds_reported", "operating_hours_measurement_time_us", "operating_hours_basis"]
        + (FLEET_FIELDS if fleet is not None else []))
    for index, record in enumerate(records):
        reported = fleet is None or "Hour" in record
        if reported:
            hours = quantity(record["Hour"], "engine hours")
            seconds = hours * 3600
            if hours < 0 or not math.isfinite(seconds):
                raise ValueError("invalid cumulative operating hours")
            stamp = utc_microseconds(record["datetime"])
        else:
            # Reported absence: no value, no time, and never a zero reading.
            seconds = stamp = None
        row = [capture_time_us, "hex:" + text.encode().hex() if index == 0 else "", digest, index,
            "hex:" + equipment_id.encode().hex(), seconds, stamp,
            "REPORTED_LIFETIME_ENGINE_TIME_NOT_WORK_OR_CONTINUITY" if reported else "HOUR_RECORD_NOT_REPORTED"]
        if fleet is not None:
            row += [fleet["snapshot_time_us"], fleet["version"], record["equipment_index"]]
            row += ["hex:" + record[field].encode().hex() if field in record else None
                    for field in ("EquipmentID", "SerialNumber", "PIN", "OEMName", "Model")]
            row.append("REPORTED_EQUIPMENT_HOUR_RECORD" if reported else "HOUR_RECORD_NOT_REPORTED_NOT_ZERO")
        writer.writerow(["" if value is None else value for value in row])
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("operating-hours output exceeds bound")
    report = dict(records=len(records), source_sha256=digest, source_format=layout,
        links_retained_not_followed=links, collection_scope="SUPPLIED_PAGE_ONLY",
        unsupported_root_elements=unsupported, clock="Unknown", equipment_identity="CALLER_DECLARED_NOT_AUTHENTICATED")
    if fleet is not None:
        report.update(unsupported_root_elements=fleet["unsupported_root_elements"],
            equipment_records=len(records),
            equipment_with_hour_record=len(records) - fleet["absent_hour_records"],
            equipment_without_hour_record=fleet["absent_hour_records"],
            unselected_metric_elements=fleet["unselected_metric_elements"],
            unknown_elements=fleet["unknown_elements"], equipment_bound=MAX_FLEET_EQUIPMENT,
            fleet_snapshot_time_us=fleet["snapshot_time_us"], fleet_version_reported=fleet["version"],
            equipment_provenance="REPORTED_HEADER_FIELDS_NOT_AUTHENTICATED_IDENTITY_OR_VEHICLE",
            selected_schema_reference="element names and namespace from ISO15143-3 20190501 common.xsd"
                " sha256:03669def0875f75057bbe96162cdcfd534f383cb7433ba8c302b6efaa4059d0c;"
                " the input is not validated against the XSD at conversion time")
    return output.getvalue(), report


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
    parser.add_argument("--arrays", action="store_true",
                        help="explicit saved 1-D UaType11 Double arrays; one ordered row per element, "
                             "no component, axis or frame meaning is taken from an index")
    parser.add_argument("--source-format", choices=("opcua", "sensorthings-1.1-celsius", SENSORTHINGS_PAGES_FORMAT, "har-1.2-sensorthings", "har-1.2-response-status", "onvif-ptz-status-1812", *AEMP_FORMATS), default="opcua")
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
    limit = 64 * 1024 * 1024 if args.source_format == SENSORTHINGS_PAGES_FORMAT else 16 * 1024 * 1024
    with args.input.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input exceeds bound")
    if args.source_format == "har-1.2-response-status":
        if args.node is not None or args.unit is not None or args.boolean or args.arrays or args.observed_property is not None or args.capture_time_us is not None:
            raise ValueError("unrelated options do not qualify HAR outcomes")
        output, report = convert_har_status(raw.decode("utf-8"))
    elif args.source_format == "onvif-ptz-status-1812":
        if args.node is not None or args.unit is not None or args.boolean or args.arrays or args.observed_property is not None:
            raise ValueError("unrelated options do not qualify PTZ status")
        output, report = convert_onvif_status(raw.decode("utf-8"), args.profile_id, args.capture_time_us)
    elif args.source_format in AEMP_FORMATS:
        if args.node is not None or args.unit is not None or args.boolean or args.arrays or args.observed_property is not None:
            raise ValueError("unrelated options do not qualify operating hours")
        output, report = convert_aemp_hours(raw.decode("utf-8"), args.equipment_id, args.capture_time_us, args.source_format)
    elif args.source_format == "opcua":
        if args.observed_property is not None or args.capture_time_us is not None:
            raise ValueError("SensorThings options require explicit format")
        output, report = convert(raw.decode("utf-8"), args.node, args.unit, boolean=args.boolean,
                                 arrays=args.arrays)
    else:
        if args.node is not None or args.unit is not None or args.boolean or args.arrays:
            raise ValueError("OPC options do not qualify SensorThings units")
        if args.source_format == "har-1.2-sensorthings":
            if args.capture_time_us is not None:
                raise ValueError("HAR request clock must not be overridden")
            output, report = convert_har_sensorthings(raw.decode("utf-8"), args.har_entry, args.observed_property)
        elif args.source_format == SENSORTHINGS_PAGES_FORMAT:
            output, report = sensorthings_saved_pages(raw.decode("utf-8"), args.observed_property,
                                                      args.capture_time_us)
        else:
            output, report = convert_sensorthings(raw.decode("utf-8"), args.observed_property, args.capture_time_us)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(output)
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
