#!/usr/bin/env python3
"""Saved PCAP interval to existing ICMP Echo meanings; no live capture."""
import argparse
import csv
import io
from pathlib import Path
import stat
import sys

try:
    from scripts.convert_imc_sensors import convert_icmp_echo, read_pcap_window
    from scripts.convert_udp_payload_window import CSV_LIMIT, report_text
except ModuleNotFoundError:
    from convert_imc_sensors import convert_icmp_echo, read_pcap_window
    from convert_udp_payload_window import CSV_LIMIT, report_text


def convert_path(path, *, start_us, stop_us):
    """Pair only inside the explicit window and bind all indexes to original capture."""
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("regular saved PCAP required")
    data, indexes, window = read_pcap_window(path, start_us, stop_us)
    after = path.stat()
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                               value.st_mtime_ns, value.st_ctime_ns)
    if signature(before) != signature(after):
        raise ValueError("PCAP source changed during interval read")
    text, report = convert_icmp_echo(data)
    if (report["packets"] != len(indexes) or window["selected_packets"] != len(indexes)
            or report["selected_packets"] + report["unselected_packets"] != len(indexes)
            or report["accounted_selected_packets"] != report["selected_packets"]):
        raise ValueError("ICMP window accounting mismatch")

    def original_index(value):
        if value == "":
            return ""
        number = int(value)
        if not 0 <= number < len(indexes):
            raise ValueError("ICMP packet index outside selected interval")
        return indexes[number]

    report["derived_capture_sha256"] = report["source_sha256"]
    report["source_sha256"] = window["source_sha256"]
    window["original_packet_indexes"] = indexes
    report["capture_window"] = window
    report["packet_count_scope"] = "SELECTED_CAPTURE_INTERVAL_ONLY"
    report["pair_scope"] = "SELECTED_CAPTURE_INTERVAL_ONLY"
    report["source_binding"] = ("original capture SHA256 plus global packet indexes; original PCAP required; "
        "outside payloads not decoded; derived capture not a new recording; no atomic snapshot guarantee")
    report["clock_scope"] = ("original capture microseconds unchanged; no new wall-clock anchor; common "
        "relative time is local to each CSV's first observation, not relative to full capture or another window")
    report["pair_scope_basis"] = ("IN_CAPTURE statuses refer to selected interval; boundary-spanning exchanges "
        "remain unpaired; PAIRED does not establish whole-capture key uniqueness or physical packet identity")
    reader = csv.DictReader(io.StringIO(text))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in reader:
        for field in ("pcap_packet_index", "icmp_request_packet_index", "icmp_reply_packet_index"):
            row[field] = original_index(row[field])
        row["pcap_source_sha256"] = window["source_sha256"]
        writer.writerow(row)
        count += 1
        if output.tell() > CSV_LIMIT:
            raise ValueError("ICMP window CSV exceeds bound; select a smaller interval")
    if count != report["decoded_records"]:
        raise ValueError("ICMP observation count mismatch")
    result = output.getvalue()
    if len(result.encode("utf-8")) > CSV_LIMIT:
        raise ValueError("ICMP window CSV exceeds bound; select a smaller interval")
    report_text(report)
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--start-us", type=int, required=True)
    parser.add_argument("--stop-us", type=int, required=True)
    args = parser.parse_args()
    try:
        output, report = convert_path(args.input, start_us=args.start_us, stop_us=args.stop_us)
        encoded_report = report_text(report)
        args.output_directory.mkdir(parents=False, exist_ok=False)
        with (args.output_directory / "observations.csv").open("x", encoding="utf-8", newline="") as stream:
            stream.write(output)
        with (args.output_directory / "report.json").open("x", encoding="utf-8", newline="") as stream:
            stream.write(encoded_report)
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"conversion refused or output incomplete: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
