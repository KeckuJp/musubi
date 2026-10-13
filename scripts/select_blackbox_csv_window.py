"""Bounded saved decoded-Blackbox selection; no raw decoder or unit inference."""
import argparse
import csv
import io
import json
from pathlib import Path

from convert_recorded_angles_csv import convert_selected_csv_path

RESERVED = {"source_file_sha256", "source_record_index", "source_header_hex",
            "source_record_hex"}


def selected_native(text):
    lines = text.splitlines()
    names = next(csv.reader([lines[0]]))
    normalized = [name.strip().split(" (")[0] for name in names]
    if RESERVED.intersection(normalized) or len(set(normalized)) != len(normalized):
        raise ValueError("ambiguous or reserved native column")
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([*names, "source_header_hex", "source_record_hex"])
    for line in lines[1:]:
        writer.writerow([*next(csv.reader([line])), lines[0].encode().hex(),
                         line.encode().hex()])
    return out.getvalue()


def convert_path(path, *, start_record, stop_record):
    result, report = convert_selected_csv_path(
        path, time_field="time (us)", required_fields=(), transform=selected_native,
        label="decoded Blackbox", start_record=start_record, stop_record=stop_record)
    report["schema"] = "blackbox-decoded-csv-record-selection/v1"
    report["meaning_basis"] = "caller-qualified-decoder-and-reader-profile; no unit inference"
    report["source_hex_basis"] = "original UTF-8 line excluding LF/CRLF terminator"
    report["downstream"] = "BlackboxCsvReader; onboard plus configured available channels"
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--start-record", required=True, type=int)
    parser.add_argument("--stop-record", required=True, type=int)
    args = parser.parse_args()
    try:
        if (args.output.resolve() == args.report.resolve()
                or any(p.exists() or p.is_symlink() for p in (args.output, args.report))):
            raise ValueError("distinct new output/report paths required")
        output, report = convert_path(args.input, start_record=args.start_record,
                                      stop_record=args.stop_record)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(output)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(2, f"refused: {exc}\n")


if __name__ == "__main__":
    main()
