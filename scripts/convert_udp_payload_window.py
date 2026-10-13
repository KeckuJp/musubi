"""Explicit saved-PCAP interval -> existing IMC/MWV meanings; no live transport."""
import argparse
import csv
import io
import json
from pathlib import Path
import sys

try:
    from scripts.convert_imc_sensors import convert_pcap, read_pcap_window, qualify_xr620_depth, validate_writer_flow
except ModuleNotFoundError:
    from convert_imc_sensors import convert_pcap, read_pcap_window, qualify_xr620_depth, validate_writer_flow

CSV_LIMIT = 128 * 1024**2
REPORT_LIMIT = 32 * 1024**2


def report_text(report):
    """Bound the actual saved encoding, including selection accounting."""
    text = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    if len(text.encode("utf-8")) > REPORT_LIMIT:
        raise ValueError("packet window report exceeds bound; select a smaller interval")
    return text


def convert_path(path, destination_port, *, start_us, stop_us, payload_format="imc", single_vlan=False,
                 water_velocity=False, writer_declaration=None, writer_flow=None, estimated_state=False):
    """Compose unchanged scanner and payload decoder; bind output to original input."""
    if payload_format not in ("imc", "nmea-wind"):
        raise ValueError("explicit supported packet payload required")
    if type(destination_port) is not int or not 1 <= destination_port <= 65535:
        raise ValueError("explicit UDP destination port required")
    if type(water_velocity) is not bool or type(estimated_state) is not bool:
        raise ValueError("water_velocity requires bool")
    if writer_declaration is not None:
        validate_writer_flow(writer_flow)
        if writer_flow["udp_destination_port"] != destination_port:
            raise ValueError("writer flow differs from selected port")
    elif writer_flow is not None:
        raise ValueError("flow binding requires a writer declaration")
    if payload_format != "imc" and (water_velocity or estimated_state or writer_declaration is not None):
        raise ValueError("writer and water options require IMC")
    data, indexes, window = read_pcap_window(path, start_us, stop_us)
    text, report = convert_pcap(data, destination_port, payload_format=payload_format,
                                single_vlan=single_vlan, water_velocity=water_velocity,
                                estimated_state=estimated_state,
                                _retain_flow=writer_declaration is not None)
    if (report["packets"] != len(indexes) or window["selected_packets"] != len(indexes)
            or report["selected_packets"] + report["unselected_packets"] != len(indexes)):
        raise ValueError("packet interval accounting mismatch")

    def original_index(value):
        if type(value) is not int or not 0 <= value < len(indexes):
            raise ValueError("packet index outside selected interval")
        return indexes[value]

    original_digest = window["source_sha256"]
    report["derived_capture_sha256"] = report["source_sha256"]
    report["source_sha256"] = original_digest
    for entry in report["selected_sources"] + report.get("vlan_unselected", []):
        local = entry["packet_index"]
        entry["packet_index"] = original_index(local)
        entry["window_packet_index"] = local
    window["original_packet_indexes"] = indexes
    report["capture_window"] = window
    report["packet_count_scope"] = "SELECTED_CAPTURE_INTERVAL_ONLY"
    report["source_binding"] = ("whole original capture SHA256 plus global original packet index; "
                                "exact selected frames in report; original PCAP required; "
                                "not authentication or atomic snapshot proof; byte offsets not supplied")
    report["payload_format"] = payload_format
    report["udp_destination_port"] = destination_port

    reader = csv.DictReader(io.StringIO(text))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in reader:
        local = int(row["pcap_packet_index"])
        row["pcap_packet_index"] = original_index(local)
        row["pcap_source_sha256"] = original_digest
        writer.writerow(row)
        count += 1
        if output.tell() > CSV_LIMIT:
            raise ValueError("packet window CSV exceeds bound; select a smaller interval")
    if count != report["decoded_records"]:
        raise ValueError("decoded observation count mismatch")
    result = output.getvalue()
    if len(result.encode("utf-8")) > CSV_LIMIT:
        raise ValueError("packet window CSV exceeds bound; select a smaller interval")
    result, report = qualify_xr620_depth(result, report, writer_declaration,
        original_digest.removeprefix("sha256:"), flow=writer_flow)
    report_text(report)
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--udp-destination-port", type=int, required=True)
    parser.add_argument("--start-us", type=int, required=True)
    parser.add_argument("--stop-us", type=int, required=True)
    parser.add_argument("--payload-format", choices=("imc", "nmea-wind"), default="imc")
    parser.add_argument("--single-vlan", action="store_true")
    parser.add_argument("--estimated-state", action="store_true")
    args = parser.parse_args()
    try:
        output, report = convert_path(args.input, args.udp_destination_port,
            start_us=args.start_us, stop_us=args.stop_us, payload_format=args.payload_format,
            single_vlan=args.single_vlan, estimated_state=args.estimated_state)
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
