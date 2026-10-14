"""Join saved ELRS reports to caller-declared settings; never infer actual configuration."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from scripts.convert_crsf_link import convert as convert_link, LIMIT
from scripts.convert_elrs_settings import convert_elrs_settings, SOURCE_FORMAT, unique_object, RATE_TABLES

BASIS = "CALLER_DECLARED_RELATION_NOT_DEVICE_AUTHENTICATION_OR_TIME_SYNC"
KEYS = {"settings_sha256", "capture_sha256", "session_label", "model_index", "radio", "start_record", "stop_record", "settings_capture_time_us"}


def convert(settings, capture, binding):
    if not isinstance(binding, dict) or set(binding) != KEYS:
        raise ValueError("exact binding fields required")
    for key, text in (("settings_sha256", settings), ("capture_sha256", capture)):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if binding[key] != digest:
            raise ValueError("binding input digest mismatch")
    if not isinstance(binding["session_label"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", binding["session_label"]):
        raise ValueError("bounded caller session label required")
    model, start, stop = (binding[k] for k in ("model_index", "start_record", "stop_record"))
    if not isinstance(binding["radio"], str) or binding["radio"] not in RATE_TABLES:
        raise ValueError("explicit qualified radio required")
    if any(type(v) is not int for v in (model, start, stop)) or not 0 <= model < 64 or not 0 <= start < stop <= 100000:
        raise ValueError("finite model and record interval required")
    setting_csv, setting_report = convert_elrs_settings(settings, source_format=SOURCE_FORMAT,
        capture_time_us=binding["settings_capture_time_us"], role="tx", radio=binding["radio"])
    chosen = {bytes.fromhex(row["elrs_setting_path_hex"][4:]).decode(): row
              for row in csv.DictReader(io.StringIO(setting_csv)) if row["elrs_model_index"] == str(model)}
    prefix = f"config.model.{model}."
    if not chosen:
        raise ValueError("declared model absent")
    rate, ratio = chosen.get(prefix + "packet-rate", {}), chosen.get(prefix + "telemetry-ratio", {})
    if len(capture.encode("utf-8")) > LIMIT:
        raise ValueError("capture exceeds existing bound")
    source = csv.DictReader(io.StringIO(capture))
    if not source.fieldnames or "__binding_index" in source.fieldnames or len(set(source.fieldnames)) != len(source.fieldnames):
        raise ValueError("unambiguous capture header required")
    derived = io.StringIO(); writer = csv.DictWriter(derived, fieldnames=source.fieldnames + ["__binding_index"], lineterminator="\n")
    writer.writeheader(); count = 0
    for index, row in enumerate(source):
        if index >= 100000 or None in row or any(v is None for v in row.values()):
            raise ValueError("capture row shape or count invalid")
        count += 1
        if start <= index < stop:
            writer.writerow(dict(row, __binding_index=index))
    if stop > count:
        raise ValueError("binding interval exceeds capture")
    link_csv, link_report = convert_link(derived.getvalue(), "link")
    rows = csv.DictReader(io.StringIO(link_csv))
    extra = ["binding_session_label", "binding_basis", "binding_model_index", "binding_source_record_index",
             "settings_sha256", "capture_sha256", "settings_capture_time_us", "settings_clock_basis",
             "packet_rate_configured_hz", "packet_rate_status", "telemetry_ratio_denominator", "telemetry_ratio_status"]
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=rows.fieldnames + extra, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        if row["destination_address"] != "234":
            raise ValueError("fixed handset writer destination required")
        original = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
        row.update(binding_session_label=binding["session_label"], binding_basis=BASIS,
            binding_model_index=model, binding_source_record_index=original["__binding_index"],
            settings_sha256=binding["settings_sha256"], capture_sha256=binding["capture_sha256"],
            settings_capture_time_us=binding["settings_capture_time_us"], settings_clock_basis="Unknown",
            packet_rate_configured_hz=rate.get("packet_rate_configured_hz", ""),
            packet_rate_status=rate.get("elrs_value_status", "NOT_REPORTED"),
            telemetry_ratio_denominator=ratio.get("telemetry_ratio_denominator", ""),
            telemetry_ratio_status=ratio.get("elrs_value_status", "NOT_REPORTED"))
        writer.writerow(row)
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("output exceeds bound")
    return output.getvalue(), {"binding": binding, "basis": BASIS, "settings": setting_report,
        "link": link_report, "source_records": count, "unselected_records": count-(stop-start),
        "nonclaims": "No configuration applied, device identity, synchronized clocks, RF diagnosis or observed rate inferred"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path); parser.add_argument("capture", type=Path)
    parser.add_argument("binding", type=Path); parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        for path in (args.settings, args.capture, args.binding):
            if path.stat().st_size > LIMIT:
                raise ValueError("input exceeds bound")
        text, report = convert(args.settings.read_bytes().decode("utf-8"), args.capture.read_bytes().decode("utf-8"),
            json.loads(args.binding.read_bytes().decode("utf-8"), object_pairs_hook=unique_object))
        args.output.mkdir()
        (args.output / "observations.csv").write_text(text)
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
    except (ValueError, OSError, csv.Error):
        parser.exit(2, "Saved ELRS context failed; no successful comparison claim\n")


if __name__ == "__main__":
    main()
