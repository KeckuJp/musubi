#!/usr/bin/env python3
"""Explicit saved PCAP window -> existing caller-local RTPS interpretation."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

try:
    from scripts.convert_imc_sensors import read_pcap_window
    from scripts.convert_rtps_data import (convert_pcap_rtps, encoded, read_bounded,
        unique_object, LIMIT, MAX_BINDING_BYTES, MAX_REPORT_BYTES, MAX_SOURCE_BYTES)
except ModuleNotFoundError:
    from convert_imc_sensors import read_pcap_window
    from convert_rtps_data import (convert_pcap_rtps, encoded, read_bounded,
        unique_object, LIMIT, MAX_BINDING_BYTES, MAX_REPORT_BYTES, MAX_SOURCE_BYTES)


def convert_path(path, destination_port, *, start_us, stop_us, binding):
    """Bind caller to whole bytes, decoder to derived bytes; preserve both scopes."""
    data, indexes, window = read_pcap_window(path, start_us, stop_us)
    original_sha = window["source_sha256"]
    if not isinstance(binding, dict) or binding.get("source_input_sha256") != original_sha:
        raise ValueError("binding must identify original whole capture")
    derived_sha = "sha256:" + hashlib.sha256(data).hexdigest()
    derived_binding = {**binding, "source_input_sha256": derived_sha}
    text, report = convert_pcap_rtps(data, destination_port, binding=derived_binding)
    if report["source_packets"] != len(indexes) or len(report["packets"]) != len(indexes):
        raise ValueError("selected interval packet accounting mismatch")

    def original_index(value):
        if type(value) is not int or not 0 <= value < len(indexes):
            raise ValueError("packet index outside selected interval")
        return indexes[value]

    for packet in report["packets"]:
        local = packet["index"]
        packet["index"] = original_index(local)
        packet["window_packet_index"] = local
    report.update(binding=dict(binding), derived_binding=derived_binding,
        derived_capture_sha256=derived_sha, capture_window=window,
        source_binding="Original whole SHA and global packet index; original PCAP required; not authentication",
        packet_count_scope="SELECTED_CAPTURE_INTERVAL_ONLY")
    window["original_packet_indexes"] = indexes
    output = io.StringIO(newline="")
    reader = csv.DictReader(io.StringIO(text))
    writer = csv.DictWriter(output, reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in reader:
        local = int(row["rtps_packet_index"])
        index = original_index(local)
        source = json.loads(bytes.fromhex(row["source_record_hex"][4:]).decode("utf-8"))
        if source["packet_index"] != local or source["source_input_sha256"] != derived_sha:
            raise ValueError("decoded source binding mismatch")
        source.update(packet_index=index, window_packet_index=local,
                      source_input_sha256=original_sha, derived_capture_sha256=derived_sha)
        retained = encoded(source)
        if len(retained) > MAX_SOURCE_BYTES:
            raise ValueError("retained source record exceeds bound")
        row.update(rtps_packet_index=index, rtps_capture_sha256=original_sha,
                   source_record_hex="hex:" + retained.hex())
        writer.writerow(row)
        count += 1
        if output.tell() > LIMIT:
            raise ValueError("window CSV exceeds bound")
    if count != report["output_records"] or count != report["selected_data"]:
        raise ValueError("selected observation accounting mismatch")
    result = output.getvalue()
    if len(result.encode("utf-8")) > LIMIT or len(encoded(report)) > MAX_REPORT_BYTES:
        raise ValueError("window output exceeds bound")
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--start-us", type=int, required=True)
    parser.add_argument("--stop-us", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output directory already exists")
        raw = read_bounded(args.binding, MAX_BINDING_BYTES)
        binding = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        text, report = convert_path(args.input, args.port, start_us=args.start_us,
                                   stop_us=args.stop_us, binding=binding)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "binding.json").write_bytes(raw)
        (args.output / "observations.csv").write_text(text, encoding="utf-8")
        (args.output / "report.json").write_bytes(encoded(report) + b"\n")
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(2, f"RTPS saved window refused or output incomplete: {exc}\n")


if __name__ == "__main__":
    main()
