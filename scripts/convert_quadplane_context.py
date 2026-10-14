"""Attach latest recorded Q_ENABLE context to fixed Plane MODE reports, never active configuration."""
import argparse
from bisect import bisect_right
from collections import Counter
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re

try:
    from scripts import normalize_ardupilot_report as normalizer
except ModuleNotFoundError:
    import normalize_ardupilot_report as normalizer

LIMIT = normalizer.LIMIT
FIELDS = ["quadplane_context_status", "quadplane_q_enable_reported", "quadplane_parameter_time_us",
          "quadplane_parameter_record_indices_hex", "quadplane_parameter_records_hex",
          "quadplane_next_parameter_report_us", "quadplane_source_input_sha256",
          "quadplane_source_profile_sha256", "quadplane_session_id_hex",
          "quadplane_session_basis", "quadplane_context_basis", "quadplane_q_enable_setting_reported"]
REPORTED_SETTINGS = {Decimal(0): "DISABLE", Decimal(1): "ENABLE", Decimal(2): "ENABLE_VTOL_AUTO"}
SESSION_BASIS = "CALLER_DECLARED_SINGLE_BOOT_SAME_SAVED_REPORT_NOT_AUTHENTICATED_OR_CAPTURE_COMPLETENESS"
CONTEXT_BASIS = ("LATEST_RECORDED_PARAMETER_CONTEXT_ONLY_NOT_ACTIVE_CONFIGURATION_OR_VTOL_STATE_"
                 "PLANE_4_5_5_d6ebfc21844e2bc0ab28c8c688b8dae94abccf5a_"
                 "Q_ENABLE_REBOOT_REQUIRED_NOT_ACTIVATION_TIME_MODE25_NOT_ENABLEMENT_PROOF")


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def hex_json(value):
    return "hex:" + json.dumps(value, ensure_ascii=False, allow_nan=False,
                                separators=(",", ":")).encode().hex()


def source_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("saved report input/profile SHA256 identity required")
    return value


def convert(raw, *, vehicle, session_id, single_boot_session):
    if not isinstance(raw, bytes) or not raw or len(raw) > LIMIT:
        raise ValueError("bounded saved reader report required")
    if vehicle != "plane-4.5.5":
        raise ValueError("only the fixed plane-4.5.5 schema is qualified")
    if single_boot_session is not True:
        raise ValueError("explicit caller single boot session declaration required")
    if (not isinstance(session_id, str) or not 1 <= len(session_id.encode()) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in session_id)):
        raise ValueError("bounded explicit session identity required")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=normalizer.unique_object,
                          parse_constant=normalizer.reject_constant)
    if not isinstance(document, dict) or not isinstance(document.get("profile_identity"), dict):
        raise ValueError("saved report profile identity required")
    input_sha = source_digest(document.get("input_sha256"))
    profile_sha = source_digest(document["profile_identity"].get("source_sha256"))
    # Existing normalization validates record shape, required fields and fixed meanings.
    mode_csv, mode_report = normalizer.convert(raw, mode_vehicle=vehicle)
    records = document["source_records"]
    previous = None
    for record in records:
        data = record["data"]
        if "TimeUS" not in data:
            continue  # Untimed metadata is retained, not assigned a clock or joined.
        time = data["TimeUS"]
        if type(time) is not int or not 0 <= time <= 2**63-1:
            raise ValueError("source TimeUS requires unsigned integer boot microseconds")
        if previous is not None and time < previous:
            raise ValueError("decreasing source boot time; explicitly split sessions")
        previous = time
    parameter_rows = []
    if any(record["meta"]["type"] == "PARM" for record in records):
        parameter_csv, _ = normalizer.convert(raw, mode_vehicle=vehicle,
                                             record="parm-plane-4.5.5",
                                             preserve_nonfinite_default=True)
        parameter_rows = [row for row in csv.DictReader(io.StringIO(parameter_csv))
                          if row["Name"] == "Q_ENABLE"]
    groups = {}
    for row in parameter_rows:
        groups.setdefault(int(row["TimeUS"]), []).append(row)
    times = sorted(groups)
    contexts = []
    for index, time in enumerate(times):
        rows = groups[time]
        indices = [int(row["source_report_record_index"]) for row in rows]
        distinct = {Decimal(row["Value"]) for row in rows}
        value = rows[0]["Value"] if len(distinct) == 1 else ""
        status = ("CONFLICTING_PARAMETER_REPORTS" if len(distinct) != 1 else
                  "LATEST_PRIOR_REPORTED_0_OR_1" if next(iter(distinct)) in (0, 1) else
                  "LATEST_PRIOR_REPORTED_2" if next(iter(distinct)) == 2 else
                  "UNINTERPRETED_PARAMETER_VALUE")
        contexts.append(dict(quadplane_context_status=status, quadplane_q_enable_reported=value,
            quadplane_q_enable_setting_reported=REPORTED_SETTINGS.get(next(iter(distinct)), "") if len(distinct) == 1 else "",
            quadplane_parameter_time_us=str(time),
            quadplane_parameter_record_indices_hex=hex_json(indices),
            quadplane_parameter_records_hex=hex_json([records[i] for i in indices]),
            quadplane_next_parameter_report_us=str(times[index+1]) if index+1 < len(times) else ""))
    parsed = csv.DictReader(io.StringIO(mode_csv))
    header = parsed.fieldnames
    if any(name in header for name in FIELDS):
        raise ValueError("reserved context column in source MODE record")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=header + FIELDS, lineterminator="\n")
    writer.writeheader()
    output_bytes = len(stream.getvalue().encode())
    statuses = Counter()
    for row in parsed:
        time = int(row["TimeUS"])
        group = bisect_right(times, time) - 1
        context = (contexts[group].copy() if group >= 0 else
                   dict(quadplane_context_status="NO_PRIOR_PARAMETER_REPORT"))
        if group >= 0 and times[group] == time:
            context["quadplane_context_status"] = "SAME_TIMESTAMP_UNORDERED"
            context["quadplane_q_enable_setting_reported"] = ""
        statuses[context["quadplane_context_status"]] += 1
        row.update(context, quadplane_source_input_sha256=input_sha,
                   quadplane_source_profile_sha256=profile_sha,
                   quadplane_session_id_hex="hex:" + session_id.encode().hex(),
                   quadplane_session_basis=SESSION_BASIS, quadplane_context_basis=CONTEXT_BASIS)
        # Count actual serialized bytes without repeatedly encoding the whole output.
        scratch = io.StringIO(newline="")
        csv.DictWriter(scratch, fieldnames=header + FIELDS, lineterminator="\n").writerow(row)
        serialized = scratch.getvalue()
        output_bytes += len(serialized.encode())
        if output_bytes > LIMIT:
            raise ValueError("context output exceeds byte bound")
        stream.write(serialized)
    output = stream.getvalue()
    mode_count = mode_report["selected_records"]
    return output, dict(schema="quadplane-reported-context/v1", source_report_sha256=digest(raw),
        source_input_sha256=input_sha, source_profile_sha256=profile_sha,
        source_records=len(records), mode_records=mode_count, q_enable_records=len(parameter_rows),
        other_records=len(records)-mode_count-len(parameter_rows), context_status_counts=dict(statuses),
        output_bytes=output_bytes, output_sha256=digest(output.encode()), session_basis=SESSION_BASIS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--vehicle", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--single-boot-session", action="store_true")
    args = parser.parse_args()
    try:
        if not args.input.is_file():
            raise ValueError("saved regular report file required")
        with args.input.open("rb") as source:
            raw = source.read(LIMIT+1)
        output, report = convert(raw, vehicle=args.vehicle, session_id=args.session_id,
                                 single_boot_session=args.single_boot_session)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (args.output_directory / "source-report.json").write_bytes(raw)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        parser.exit(2, "Saved QuadPlane context conversion refused; no completion claim\n")


if __name__ == "__main__":
    main()
