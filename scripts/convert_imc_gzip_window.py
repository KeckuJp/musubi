"""Bounded saved gzip LSF -> existing native interval and common meanings; no acquisition."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_imc_sensors as imc

MAX_COMPRESSED_BYTES = 64 * 1024**2
MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_MEMBERS = 1024
CHUNK_BYTES = 1024**2
MAX_REPORT_BYTES = 64 * 1024**2


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def expand(path, destination):
    """All complete gzip members; every decode call has a positive, bounded output budget."""
    metadata = path.stat()
    if (not stat.S_ISREG(metadata.st_mode) or path.parts[1:2] in (("sys",), ("proc",), ("dev",))
            or not 0 < metadata.st_size <= MAX_COMPRESSED_BYTES):
        raise ValueError("saved regular gzip input within compressed byte bound required")
    compressed_hash, expanded_hash = hashlib.sha256(), hashlib.sha256()
    compressed_size, expanded_size, members = 0, 0, 0
    decoder, pending = None, b""
    with path.open("rb") as source, destination.open("xb") as target:
        before = os.fstat(source.fileno())
        if signature(before) != signature(metadata):
            raise ValueError("gzip source changed before read")
        while True:
            if not pending:
                pending = source.read(CHUNK_BYTES)
                if not pending:
                    if decoder is not None:
                        raise ValueError("truncated gzip member")
                    break
                compressed_size += len(pending)
                if compressed_size > MAX_COMPRESSED_BYTES:
                    raise ValueError("compressed gzip byte bound exceeded")
                compressed_hash.update(pending)
            if decoder is None:
                members += 1
                if members > MAX_MEMBERS:
                    raise ValueError("gzip member count exceeds bound")
                decoder = zlib.decompressobj(31)
            try:
                part = decoder.decompress(pending, min(CHUNK_BYTES, MAX_EXPANDED_BYTES - expanded_size + 1))
            except zlib.error as error:
                raise ValueError("invalid gzip member or trailing data") from error
            expanded_size += len(part)
            if expanded_size > MAX_EXPANDED_BYTES:
                raise ValueError("gzip expansion exceeds bound")
            if target.write(part) != len(part):
                raise OSError("short temporary LSF write")
            expanded_hash.update(part)
            if decoder.eof:
                pending, decoder = decoder.unused_data, None
            else:
                pending = decoder.unconsumed_tail
        if (compressed_size != before.st_size
                or signature(os.fstat(source.fileno())) != signature(before)):
            raise ValueError("gzip source changed during read")
    if not members or not expanded_size:
        raise ValueError("gzip contains no native LSF bytes")
    return dict(compressed_sha256=compressed_hash.hexdigest(), compressed_bytes=compressed_size,
        expanded_lsf_sha256=expanded_hash.hexdigest(), expanded_lsf_bytes=expanded_size,
        gzip_members=members)


def convert(path, *, start_record, stop_record, water_velocity=False, schema=None, estimated_state=False,
            source_id=None):
    if (type(start_record) is not int or type(stop_record) is not int
            or not 0 <= start_record < stop_record <= imc.IMC_WINDOW_RECORD_LIMIT):
        raise ValueError("explicit bounded nonempty native interval required")
    if schema not in (None, imc.NEPTUS_SCHEMA) or type(water_velocity) is not bool:
        raise ValueError("unsupported or incompatible IMC schema selection")
    imc.estimated_selection(estimated_state, schema)
    path = Path(path).resolve()
    with tempfile.TemporaryDirectory(prefix="musubi-imc-gzip-window-") as directory:
        native = Path(directory) / "expanded.lsf"
        compression = expand(path, native)
        output, report = imc.convert_path(native, start_record=start_record, stop_record=stop_record,
                                         water_velocity=water_velocity, schema=schema,
                                         estimated_state=estimated_state, source_id=source_id)
        selection = report["record_selection"]
        if (selection["source_sha256"] != compression["expanded_lsf_sha256"]
                or selection["source_bytes"] != compression["expanded_lsf_bytes"]):
            raise ValueError("expanded LSF/native scan byte binding differs")
    compression.update(original_path_hex="hex:" + str(path).encode("utf-8").hex(),
        format="RFC1952_GZIP_CONCATENATED_MEMBERS_OF_SAVED_LSF",
        container_validation="ALL_GZIP_MEMBERS_CRC_ISIZE_AND_TERMINAL_STATE_VALIDATED",
        common_source_basis="SOURCE_FILE_SHA_AND_OFFSETS_REFER_TO_EXPANDED_ORIGINAL_LSF_NOT_COMPRESSED_BYTES",
        outside_native_selection="PAYLOAD_AND_IMC_CRC_NOT_DECODED_OR_VALIDATED",
        required_artifacts=["original_compressed_source", "observations.csv", "report.json"],
        temporary_lsf="REMOVED_REPRODUCIBLE_FROM_BOUND_ORIGINAL_GZIP_NOT_NEW_RECORDING",
        source_stability="FD_STAT_SIGNATURE_CHECK_NOT_ATOMIC_SNAPSHOT_OR_HOSTILE_RACE_SANDBOX",
        bounds=dict(max_compressed_bytes=MAX_COMPRESSED_BYTES, max_expanded_bytes=MAX_EXPANDED_BYTES,
                    max_members=MAX_MEMBERS, decode_chunk_bytes=CHUNK_BYTES))
    report["gzip_source"] = compression
    if len(json.dumps(report, indent=2).encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("gzip interval report byte bound")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--start-record", required=True, type=int)
    parser.add_argument("--stop-record", required=True, type=int)
    parser.add_argument("--water-velocity", action="store_true")
    parser.add_argument("--estimated-state", action="store_true")
    parser.add_argument("--source-format", choices=[imc.NEPTUS_SCHEMA])
    parser.add_argument("--source-id", type=int, help="keep one header src system (see native window)")
    args = parser.parse_args()
    try:
        if args.output_directory.exists():
            raise ValueError("fresh output directory required")
        output, report = convert(args.input, start_record=args.start_record, stop_record=args.stop_record,
            water_velocity=args.water_velocity, schema=args.source_format, estimated_state=args.estimated_state,
            source_id=args.source_id)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"saved gzip LSF conversion refused; no successful conversion claim: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
