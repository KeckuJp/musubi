"""Fixed saved ArduSub CTUN target/estimate reports; never actuation or depth truth."""
import argparse
import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.convert_ardupilot_controller_reports import integer, retained_hex
from scripts.normalize_ardupilot_report import BASIS, reject_constant, unique_object

SOURCE_VERSION = "ardusub-4.5.7-ctun"
UNDERWATER_SOURCES = {SOURCE_VERSION: ("4.5.7", "30257f01"),
                      "ardusub-4.5.3-ctun": ("4.5.3", "80e8c23a")}
MAX_INPUT = MAX_OUTPUT = MAX_REPORT = 16 * 1024 * 1024
MAX_RECORD_BYTES = 12000
MAX_ROWS = 100000
VALUE_BASIS = "REPORTED_Z_UP_SIGNED16_TARGET_AND_RETAINED_ESTIMATE_NOT_ACTUATION_OR_PHYSICAL_TRACKING"
FRESHNESS = "UNKNOWN_CTUN_HAS_NO_VALIDITY_OR_SAMPLE_AGE"
FIELDS = ["record_time_us", "source_report_record_index", "source_document_sha256", "source_version",
          "reported_target_vertical_rate_m_s", "reported_estimated_vertical_rate_m_s",
          "vertical_rate_basis", "vertical_rate_freshness", "source_record_hex"]

ANCHOR_FIELDS = {"schema", "source_report_sha256", "source_id", "session_id", "origin_epoch_id",
    "citation", "anchor_record_index", "anchor_time_us", "anchor_z_up_m", "valid_start_us",
    "valid_stop_us", "origin_reset_times_us", "unit", "positive_direction", "reference"}
DEPTH_FIELDS = ["declared_surface_target_depth_down_m", "declared_surface_estimated_depth_down_m",
    "depth_reference_basis", "anchor_declaration_sha256", "anchor_record_index", "anchor_time_us",
    "anchor_z_up_m", "declared_source_id", "declared_session_id", "declared_origin_epoch_id",
    "anchor_valid_start_us", "anchor_valid_stop_us", "anchor_citation_hex"]


def surface_relation(raw, records, digest):
    """Validate an explicitly supplied source-bound relation, not a physical surface."""
    if not isinstance(raw, bytes) or not raw or len(raw) > 65536:
        raise ValueError("bounded surface anchor declaration bytes required")
    relation = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(relation, dict) or set(relation) != ANCHOR_FIELDS:
        raise ValueError("exact surface anchor declaration fields required")
    if (relation["schema"] != "ardusub-declared-surface/v1" or relation["source_report_sha256"] != digest
            or relation["unit"] != "m" or relation["positive_direction"] != "up"
            or relation["reference"] != "reported_ekf_origin"):
        raise ValueError("surface anchor source, schema, unit or reference mismatch")
    for key in ("source_id", "session_id", "origin_epoch_id", "citation"):
        value = relation[key]
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > 4096:
            raise ValueError("nonempty bounded surface anchor identity/citation required")
    for key in ("anchor_record_index", "anchor_time_us", "valid_start_us", "valid_stop_us"):
        integer(relation[key], key, 2**63 - 1)
    start, stop = relation["valid_start_us"], relation["valid_stop_us"]
    if start > stop or not start <= relation["anchor_time_us"] <= stop:
        raise ValueError("surface anchor outside declared origin validity")
    resets = relation["origin_reset_times_us"]
    if not isinstance(resets, list) or len(resets) > 1024:
        raise ValueError("explicit bounded origin reset boundary list required")
    for value in resets:
        integer(value, "origin reset time", 2**63 - 1)
    if resets != sorted(set(resets)) or any(start < value <= stop for value in resets):
        raise ValueError("surface anchor validity crosses an origin reset or reset list invalid")
    index = relation["anchor_record_index"]
    if index >= len(records):
        raise ValueError("surface anchor record absent")
    record = records[index]
    if (not isinstance(record, dict) or not isinstance(record.get("meta"), dict)
            or record["meta"].get("type") != "CTUN" or not isinstance(record.get("data"), dict)):
        raise ValueError("surface anchor must refer to supplied CTUN")
    data = record["data"]
    z = relation["anchor_z_up_m"]
    if (type(z) not in (int, float) or not math.isfinite(z) or type(data.get("Alt")) not in (int, float)
            or data["Alt"] != z or data.get("TimeUS") != relation["anchor_time_us"]):
        raise ValueError("surface anchor time or coordinate contradicts referenced record")
    return relation, "sha256:" + hashlib.sha256(raw).hexdigest()


def convert(raw, *, source_version, underwater_position=False, surface_anchor=None):
    """Return bounded CSV/accounting; keep the unchanged input report alongside them."""
    if source_version != SOURCE_VERSION and not (underwater_position is True and source_version in UNDERWATER_SOURCES):
        raise ValueError("explicit ardusub-4.5.7-ctun source version required")
    if type(underwater_position) is not bool:
        raise ValueError("underwater_position must be boolean")
    if surface_anchor is not None and not underwater_position:
        raise ValueError("surface anchor requires qualified underwater position mode")
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT:
        raise ValueError("bounded nonempty saved reader report required")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(document, dict) or document.get("value_basis") != BASIS:
        raise ValueError("decoded read_mavlog_json report required")
    records = document.get("source_records")
    if not isinstance(records, list) or not records:
        raise ValueError("nonempty source_records required")
    identity_indices = []
    if underwater_position:
        version, git_id = UNDERWATER_SOURCES[source_version]
        for index, record in enumerate(records):
            if (isinstance(record, dict) and isinstance(record.get("meta"), dict)
                    and isinstance(record.get("data"), dict) and record["meta"].get("type") == "MSG"):
                message = record.get("data", {}).get("Message")
                if isinstance(message, str) and re.match(r"(?:ArduCopter|ArduPlane|ArduRover|Rover|AntennaTracker) V?\d+\.\d+", message):
                    raise ValueError("conflicting vehicle firmware build in underwater source")
                if isinstance(message, str) and message.startswith("ArduSub "):
                    if not re.fullmatch(r"ArduSub V?" + re.escape(version) + r" \(" + git_id + r"\)", message):
                        raise ValueError("reported Sub build contradicts fixed underwater source")
                    identity_indices.append(index)
        if not identity_indices:
            raise ValueError("fixed Sub build MSG required for underwater position binding")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    relation = surface_relation(surface_anchor, records, digest) if surface_anchor is not None else None
    report = dict(source_document_sha256=digest, source_version=source_version,
        source_records=len(records), selected_records=0, not_selected_records=0,
        unselected_record_types={}, clock_basis="BootRelative",
        selection_basis="CALLER_DECLARED_FIXED_SOURCE_NOT_AUTHENTICATION",
        value_basis=VALUE_BASIS, freshness=FRESHNESS,
        source_retention="source-report.json is the unchanged input; retain with observations.csv")
    output = io.StringIO(newline="")
    extra_fields = ["reported_target_z_up_m", "reported_estimated_z_up_m", "position_basis",
                    "domain_binding_basis", "identity_record_indices"] if underwater_position else []
    if underwater_position:
        report.update(platform_domain="Subsurface", identity_record_indices=identity_indices,
            depth_disposition="UNQUALIFIED_WATER_SURFACE_ANCHOR_AND_ORIGIN_RESET_HISTORY",
            northbound_disposition="UNQUALIFIED_COT_NVG_MAPPING_REFUSED")
    if relation:
        report.update(depth_disposition="CONDITIONAL_DECLARED_SURFACE_NOT_MEASURED_OR_AUTHENTICATED",
            anchor_declaration_sha256=relation[1], origin_reset_basis="CALLER_SUPPLIED_BOUNDARIES_NOT_INFERRED_FROM_CTUN")
        extra_fields += DEPTH_FIELDS
    writer = csv.writer(output, lineterminator="\n"); writer.writerow(FIELDS + extra_fields)
    previous = None
    for index, record in enumerate(records):
        if (not isinstance(record, dict) or not isinstance(record.get("meta"), dict)
                or not isinstance(record.get("data"), dict)):
            raise ValueError("invalid source record shape")
        kind = record["meta"].get("type")
        if not isinstance(kind, str) or not kind or len(kind) > 256:
            raise ValueError("invalid source record type")
        if kind != "CTUN":
            counts = report["unselected_record_types"]
            counts[kind] = counts.get(kind, 0) + 1
            report["not_selected_records"] += 1
            continue
        if report["selected_records"] >= MAX_ROWS:
            raise ValueError("selected CTUN row count exceeds bound")
        data = record["data"]
        stamp = integer(data.get("TimeUS"), "CTUN.TimeUS", 2**63 - 1)
        if previous is not None and stamp < previous:
            raise ValueError("selected CTUN boot time goes backwards; split input explicitly")
        previous = stamp
        values = []
        for field in ("DCRt", "CRt"):
            value = data.get(field)
            if type(value) is not int or not -32768 <= value <= 32767:
                raise ValueError(f"CTUN.{field} must be reported signed16 integer")
            values.append(value / 100)
        retained = retained_hex(record)
        if (len(retained) - 4) // 2 > MAX_RECORD_BYTES:
            raise ValueError("selected CTUN record exceeds retention bound")
        extra = []
        if underwater_position:
            for name in ("DAlt", "Alt"):
                value = data.get(name)
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError(f"CTUN.{name} must be finite reported metres")
                extra.append(value)
            extra.extend(["REPORTED_EKF_ORIGIN_Z_UP_METRES_NOT_WATER_SURFACE_DEPTH_OR_FRESHNESS",
                "FIXED_SUB_SOURCE_AND_SAME_REPORT_BUILD_MSG_NOT_PHYSICAL_AUTHENTICATION",
                ":".join(map(str, identity_indices))])
        if relation:
            anchor, anchor_digest = relation
            if not anchor["valid_start_us"] <= stamp <= anchor["valid_stop_us"]:
                raise ValueError("selected CTUN outside declared origin validity")
            depths = [anchor["anchor_z_up_m"] - data[name] for name in ("DAlt", "Alt")]
            if not all(math.isfinite(value) for value in depths):
                raise ValueError("declared surface depth overflow")
            extra += [*depths, "CALLER_DECLARED_SURFACE_POSITIVE_DOWN_NOT_PHYSICAL_DATUM_VALIDATION",
                anchor_digest, anchor["anchor_record_index"], anchor["anchor_time_us"], anchor["anchor_z_up_m"],
                "hex:" + anchor["source_id"].encode().hex(), "hex:" + anchor["session_id"].encode().hex(),
                "hex:" + anchor["origin_epoch_id"].encode().hex(), anchor["valid_start_us"],
                anchor["valid_stop_us"], "hex:" + anchor["citation"].encode().hex()]
        writer.writerow([stamp, index, digest, source_version, *values,
                         VALUE_BASIS, FRESHNESS, retained, *extra])
        if output.tell() > MAX_OUTPUT:  # ASCII CSV: unknown Unicode is inside hex.
            raise ValueError("CTUN output exceeds bound")
        report["selected_records"] += 1
    if not report["selected_records"]:
        raise ValueError("no selected CTUN records")
    if len(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2).encode()) + 1 > MAX_REPORT:
        raise ValueError("CTUN accounting report exceeds bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_report", type=Path)
    parser.add_argument("output_directory", type=Path, help="new directory; retain all three files")
    parser.add_argument("--source-version", choices=tuple(UNDERWATER_SOURCES), required=True)
    parser.add_argument("--underwater-position", action="store_true",
                        help="require fixed in-report Sub build; use underwater-profile.toml")
    parser.add_argument("--surface-anchor", type=Path, help="explicit same-source surface relation; never inferred")
    args = parser.parse_args()
    try:
        with args.input_report.open("rb") as source:
            raw = source.read(MAX_INPUT + 1)
        anchor = None
        if args.surface_anchor is not None:
            with args.surface_anchor.open("rb") as source:
                anchor = source.read(65537)
        text, report = convert(raw, source_version=args.source_version,
                               underwater_position=args.underwater_position, surface_anchor=anchor)
        args.output_directory.mkdir()
        (args.output_directory / "source-report.json").write_bytes(raw)
        if anchor is not None:
            (args.output_directory / "surface-anchor.json").write_bytes(anchor)
        (args.output_directory / "observations.csv").write_text(text, encoding="utf-8")
        (args.output_directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"saved Sub vertical-rate conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
