"""Passive fixed-source Get_Iq pairing from saved CAN rows, never a motor verdict."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from scripts.convert_dronecan_capture import capture_rows
from scripts.convert_dbc_recorded import convert as decode
from scripts.convert_declared_expectation_check import bounded_number

SOURCE = "odrive-fw0.5.6-cansimple"
DBC_SHA256 = "4db07a4cef0ee1a87cfbfc779cabd7db3176072c688a694e3b8842c2dd9bcb55"
INPUT_LIMIT = 16 * 1024 * 1024
FRAME_LIMIT = 100000
OUTPUT_LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 64 * 1024 * 1024
FIELDS = ["record_time_us", "source_frame_index", "node_id", "frame_id",
          "iq_setpoint_a", "iq_measured_a", "setpoint_disposition", "source_data_hex",
          "source_row_hex", "source_input_sha256", "source_dbc_sha256", "producer_basis"]


def convert(data, dbc, *, source, node_id):
    if source != SOURCE or type(node_id) is not int or not 0 <= node_id <= 7:
        raise ValueError("explicit fixed source and DBC-declared node 0..7 required")
    if not isinstance(data, bytes) or not data or len(data) > INPUT_LIMIT:
        raise ValueError("empty or oversized capture")
    if (not isinstance(dbc, bytes) or len(dbc) > 2 * 1024 * 1024
            or hashlib.sha256(dbc).hexdigest() != DBC_SHA256):
        raise ValueError("exact pinned ODrive DBC bytes required")
    selected, unselected = [], []
    wanted = (node_id << 5) | 0x14
    capture = io.StringIO(newline="")
    writer = csv.writer(capture, lineterminator="\n")
    writer.writerow(["time_us", "standard_id", "data_hex"])
    count = 0
    for line, row, moment, identifier, payload in capture_rows(data.decode("utf-8"), extended=False):
        if count >= FRAME_LIMIT:
            raise ValueError("capture exceeds frame work bound")
        index = count
        count += 1
        if identifier != wanted:
            unselected.append(dict(source_frame_index=index, source=row, reason="UNSELECTED_NODE_OR_MESSAGE"))
            continue
        if len(payload) != 8:
            raise ValueError("selected Get_Iq requires exactly eight bytes")
        selected.append((index, row, moment, payload))
        writer.writerow(row)
    if not selected:
        raise ValueError("no selected Get_Iq frame; no successful adaptation")
    # Decode once. Exactly two consecutive signal rows belong to each selected input
    # frame, even when time, node, and bytes repeat. There is no time-based join.
    decoded, decoder_report = decode(capture.getvalue(), dbc)
    if (decoder_report["frames"] != len(selected) or decoder_report["decoded_frames"] != len(selected)
            or decoder_report["observations"] != 2 * len(selected) or decoder_report["unsupported"]):
        raise ValueError("selected frame decoder accounting mismatch")
    signals = iter(csv.DictReader(io.StringIO(decoded)))
    output = io.StringIO(newline="")
    out = csv.DictWriter(output, FIELDS, lineterminator="\n")
    out.writeheader()
    input_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    dbc_hash = "sha256:" + DBC_SHA256
    zero_count = 0
    for index, original, moment, payload in selected:
        values = {}
        for _ in range(2):
            signal = next(signals, None)
            if signal is None:
                raise ValueError("missing decoded signal row")
            expected = dict(record_time_us=str(moment), frame_id=str(wanted), frame_kind="standard",
                            dbc_sha256=dbc_hash, source_data_hex="hex:" + payload.hex())
            if any(signal.get(key) != value for key, value in expected.items()):
                raise ValueError("decoded signal frame provenance mismatch")
            name = signal.get("signal_name_hex")
            names = {"hex:" + key.encode().hex(): key for key in ("Iq_Setpoint", "Iq_Measured")}
            if name not in names or names[name] in values:
                raise ValueError("missing, duplicate, or unexpected pair signal")
            if not signal.get("current_a") or any(value for key, value in signal.items()
                    if key not in set(expected) | {"signal_name_hex", "current_a"}):
                raise ValueError("unexpected decoded quantity")
            # Keep the decoder's reported decimal value; only expand exponent spelling.
            # Do not add comparison arithmetic or silently round a subnormal to zero.
            plain = format(Decimal(signal["current_a"]), "f")
            bounded_number(plain, "reported Iq pair value")
            values[names[name]] = plain
        zero = Decimal(values["Iq_Setpoint"]) == 0
        zero_count += zero
        out.writerow(dict(record_time_us=moment, source_frame_index=index, node_id=node_id,
            frame_id=wanted, iq_setpoint_a=values["Iq_Setpoint"], iq_measured_a=values["Iq_Measured"],
            setpoint_disposition="ZERO_REPORTED_OR_MISSING_SUBSTITUTED" if zero else "NONZERO_REPORTED_SETPOINT",
            source_data_hex="hex:" + payload.hex(),
            source_row_hex="hex:" + json.dumps(original, separators=(",", ":")).encode().hex(),
            source_input_sha256=input_hash, source_dbc_sha256=dbc_hash, producer_basis=SOURCE))
        # All generated characters are ASCII (text source cells are hex encoded).
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("pair output exceeds comparator input byte bound")
    if next(signals, None) is not None:
        raise ValueError("unexpected extra decoded signal row")
    report = dict(frames=count, selected_frames=len(selected), unselected_frames=len(unselected),
        pairs=len(selected), decoded_signals=2 * len(selected), unselected=unselected,
        zero_setpoint_ambiguous_pairs=zero_count, source_input_sha256=input_hash,
        source_dbc_sha256=dbc_hash, producer_basis=SOURCE, node_id=node_id,
        clock="Unknown", time_basis="saved capture microseconds; no producer clock in Get_Iq",
        setpoint_basis="zero can be a reported zero or writer substitution for an absent internal setpoint",
        producer_qualification="caller-selected source; dictionary hash is byte binding, not authentication",
        physical_actuator_mismatch="NOT_DERIVED", decoder="cantools-40.7.1",
        output_bytes=output.tell())
    if len(json.dumps(report).encode()) > REPORT_LIMIT:
        raise ValueError("accounting report exceeds byte bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("capture", "dbc", "output", "report"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--source", choices=[SOURCE], required=True)
    parser.add_argument("--node-id", type=int, required=True)
    args = parser.parse_args()
    paths = [getattr(args, key).resolve() for key in ("capture", "dbc", "output", "report")]
    if len(set(paths)) != 4 or args.output.exists() or args.report.exists():
        parser.error("distinct paths and new output/report files required")
    with args.capture.open("rb") as stream:
        data = stream.read(INPUT_LIMIT + 1)
    with args.dbc.open("rb") as stream:
        dbc = stream.read(2 * 1024 * 1024 + 1)
    output, report = convert(data, dbc, source=args.source, node_id=args.node_id)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(output)
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
