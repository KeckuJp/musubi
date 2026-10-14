"""Passive saved RCOU reports at one ArduPilot source pin; never physical servo position."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.convert_ardupilot_controller_reports import integer, retained_hex
from scripts.normalize_ardupilot_report import BASIS, reject_constant, unique_object

SOURCE_VERSION = "ardupilot-a64bad1a-rcou"
MAX_INPUT = 128 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_REPORT = 16 * 1024 * 1024
MAX_RECORD_BYTES = 12000
MAX_ROWS = 100000
CHANNELS = tuple(range(1, 15))
VALUE_BASIS = "REPORTED_HAL_OUTPUT_STATE_NOT_VERIFIED_PULSE_NOT_PHYSICAL_SERVO_POSITION"
CHANNEL_BASIS = "SOURCE_CHANNEL_1_TO_14_NOT_ACTUATOR_ID_NOT_ALL_CHANNELS_VALID"
FIELDS = ["record_time_us", "source_report_record_index", "source_document_sha256",
          "source_version", "rcout_value_basis", "rcout_channel_basis",
          *[f"rcout_c{i}_reported_s" for i in CHANNELS], "source_record_hex"]


def convert(raw, *, source_version):
    """One selected RCOU record per row; the unchanged input must accompany the CSV."""
    if source_version != SOURCE_VERSION:
        raise ValueError("explicit ardupilot-a64bad1a-rcou source version required")
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT:
        raise ValueError("bounded nonempty saved reader report required")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    if not isinstance(document, dict) or document.get("value_basis") != BASIS:
        raise ValueError("decoded read_mavlog_json report required; no second wire scaling")
    records = document.get("source_records")
    if not isinstance(records, list) or not records:
        raise ValueError("nonempty source_records required")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    report = dict(source_document_sha256=digest, source_version=source_version,
                  source_records=len(records), selected_records=0, not_selected_records=0,
                  unselected_record_types={}, clock_basis="BootRelative",
                  selection_basis="CALLER_DECLARED_SOURCE_PIN_NOT_AUTHENTICATION",
                  source_retention="source-report.json is the unchanged input; retain with observations.csv",
                  value_basis=VALUE_BASIS, channel_basis=CHANNEL_BASIS)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(FIELDS)
    previous = None
    for index, record in enumerate(records):
        if (not isinstance(record, dict) or not isinstance(record.get("meta"), dict)
                or not isinstance(record.get("data"), dict)):
            raise ValueError("invalid source record shape")
        kind = record["meta"].get("type")
        if not isinstance(kind, str) or not kind or len(kind) > 256:
            raise ValueError("invalid source record type")
        if kind != "RCOU":
            counts = report["unselected_record_types"]
            counts[kind] = counts.get(kind, 0) + 1
            report["not_selected_records"] += 1
            continue
        if report["selected_records"] >= MAX_ROWS:
            raise ValueError("selected RCOU row count exceeds bound")
        data = record["data"]
        stamp = integer(data.get("TimeUS"), "RCOU.TimeUS", 2**63 - 1)
        if previous is not None and stamp < previous:
            raise ValueError("selected RCOU boot time goes backwards; split the input explicitly")
        previous = stamp
        # H has no sentinel at this pin. Zero remains a reported zero; a missing
        # channel is malformed, not zero and not proof the physical channel is off.
        values = [integer(data.get(f"C{i}"), f"RCOU.C{i}", 65535) / 1_000_000
                  for i in CHANNELS]
        retained = retained_hex(record)
        if (len(retained) - 4) // 2 > MAX_RECORD_BYTES:
            raise ValueError("selected source record exceeds retention bound")
        writer.writerow([stamp, index, digest, source_version, VALUE_BASIS, CHANNEL_BASIS,
                         *values, retained])
        # CSV is ASCII: unknown Unicode is retained as hex, so character count is
        # byte count. The original full report is stored once, not for each channel.
        if output.tell() > MAX_OUTPUT:
            raise ValueError("RCOU CSV exceeds output bound")
        report["selected_records"] += 1
    if not report["selected_records"]:
        raise ValueError("no selected RCOU records")
    if len(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2).encode()) + 1 > MAX_REPORT:
        raise ValueError("RCOU accounting report exceeds bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_report", type=Path)
    parser.add_argument("output_directory", type=Path, help="new directory; retain all three files")
    parser.add_argument("--source-version", choices=(SOURCE_VERSION,), required=True)
    args = parser.parse_args()
    try:
        with args.input_report.open("rb") as source:
            raw = source.read(MAX_INPUT + 1)
        text, report = convert(raw, source_version=args.source_version)
        args.output_directory.mkdir()
        (args.output_directory / "source-report.json").write_bytes(raw)
        (args.output_directory / "observations.csv").write_text(text, encoding="utf-8")
        (args.output_directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"saved RCOU conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
