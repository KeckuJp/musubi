"""Fixed saved Sub BIN interval to adopted session meanings; never device access."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import export_ardupilot_records as exporter
from scripts import convert_ardusub_session_window as window


def convert(path, *, start_record, stop_record, build_record, source_pin, session_id,
            single_boot_session, mavlog_reader, qualify_session_domain=False, surface_anchor=None):
    if type(qualify_session_domain) is not bool:
        raise ValueError("session domain option must be boolean")
    if source_pin not in window.session.SOURCE_PINS or single_boot_session is not True:
        raise ValueError("fixed Sub source and declared single boot session required")
    if type(build_record) is not int or not 0 <= build_record < stop_record:
        raise ValueError("preceding or selected original build index required")
    exports, original = exporter.export_window(path, tuple(exporter.SUB457_LAYOUTS),
        start_record, stop_record, sub_source_pin=source_pin)
    text = exports.get("selected", "")
    if len(text.encode()) > window.session.LIMIT:
        raise ValueError("decoded interval too large; select smaller original interval")
    records = [json.loads(line) for line in text.splitlines()]
    context_only = build_record < start_record
    if context_only:
        context, basis = exporter.export_window(path, ("MSG",), build_record, build_record+1,
                                                sub_source_pin=source_pin)
        if (basis["source_sha256"] != original["source_sha256"] or
                basis["source_bytes"] != original["source_bytes"] or
                basis["total_messages"] != original["total_messages"]):
            raise ValueError("build context and interval originals differ")
        records = [json.loads(line) for line in context.get("selected", "").splitlines()] + records
    relation = None
    if surface_anchor is not None:
        if not isinstance(surface_anchor, bytes) or not 0 < len(surface_anchor) <= 65536:
            raise ValueError("bounded native anchor bytes required")
        relation = json.loads(surface_anchor, object_pairs_hook=window.session.vertical.unique_object,
            parse_constant=window.session.vertical.reject_constant)
        expected = window.session.vertical.ANCHOR_FIELDS - {"source_report_sha256"} | {"original_bin_sha256"}
        if (not isinstance(relation, dict) or set(relation) != expected
                or relation["schema"] != "ardusub-bin-declared-surface/v1"
                or relation["original_bin_sha256"] != original["source_sha256"]
                or relation["session_id"] != session_id):
            raise ValueError("native anchor source/schema/session mismatch")
        anchor_index = relation["anchor_record_index"]
        window.session.vertical.integer(anchor_index, "native anchor index", 2**63-1)
        if not any(r["source_record_index"] == anchor_index for r in records):
            extra, basis = exporter.export_window(path, ("CTUN",), anchor_index, anchor_index+1,
                                                  sub_source_pin=source_pin)
            if any(basis[k] != original[k] for k in ("source_sha256", "source_bytes", "total_messages")):
                raise ValueError("anchor context and interval originals differ")
            records += [json.loads(line) for line in extra.get("selected", "").splitlines()]
            records.sort(key=lambda r: r["source_record_index"])
        if not any(r["source_record_index"] == anchor_index and r.get("meta", {}).get("type") == "CTUN"
                   for r in records):
            raise ValueError("native anchor must identify CTUN")
    indices = [r["source_record_index"] for r in records]
    if build_record not in indices:
        raise ValueError("build index does not identify a selected MSG")
    if indices != sorted(set(indices)):
        raise ValueError("original record order changed")
    native = ("\n".join(json.dumps(r, ensure_ascii=False, allow_nan=False) for r in records)+"\n").encode()
    if len(native) > window.session.LIMIT:
        raise ValueError("decoded interval plus context too large")
    decoded_anchor = None
    inner_start, inner_stop = 0, len(records)
    if relation is not None:
        selected_indices = [i for i, index in enumerate(indices) if start_record <= index < stop_record]
        if not selected_indices:
            raise ValueError("native anchor cannot supply missing selected observations")
        inner_start, inner_stop = selected_indices[0], selected_indices[-1]+1
        decoded_relation = dict(relation)
        decoded_relation.pop("original_bin_sha256")
        decoded_relation.update(schema="ardusub-jsonl-declared-surface/v1",
            original_jsonl_sha256="sha256:"+hashlib.sha256(native).hexdigest(),
            anchor_record_index=indices.index(anchor_index))
        decoded_anchor = json.dumps(decoded_relation, allow_nan=False).encode()
    with tempfile.TemporaryDirectory() as directory:
        selected = Path(directory)/"selected.jsonl"
        selected.write_bytes(native)
        files, report = window.convert(selected, start_record=inner_start, stop_record=inner_stop,
            build_record=indices.index(build_record), source_pin=source_pin, session_id=session_id,
            single_boot_session=single_boot_session, mavlog_reader=mavlog_reader,
            qualify_session_domain=qualify_session_domain, surface_anchor=decoded_anchor)
    for item in report["outputs"]:
        reader = csv.DictReader(io.StringIO(files[item["file"]].decode()))
        rows = list(reader)
        fields = ["original_bin_sha256", "original_bin_record_index", "original_bin_byte_offset",
                  "original_bin_record_length", "native_selection_basis"]
        if relation is not None:
            fields += ["original_bin_anchor_sha256", "native_anchor_record_index"]
        for row in rows:
            record = records[int(row["original_jsonl_record_index"])]
            if not start_record <= record["source_record_index"] < stop_record:
                raise ValueError("context produced an observation")
            row.update(original_bin_sha256=original["source_sha256"],
                original_bin_record_index=record["source_record_index"],
                original_bin_byte_offset=record["source_offset"],
                original_bin_record_length=record["source_length"],
                native_selection_basis="FIXED_FMT_DECODED_ONCE_SELECTED_INTERVAL_NOT_ALL_ORIGINAL_MEANING")
            if relation is not None:
                row.update(original_bin_anchor_sha256="sha256:"+hashlib.sha256(surface_anchor).hexdigest(),
                           native_anchor_record_index=anchor_index)
        files[item["file"]] = window.session.table(rows, reader.fieldnames+fields).encode()
        if len(files[item["file"]]) > window.session.LIMIT:
            raise ValueError("native provenance exceeds route bound; select smaller interval")
        item["sha256"] = "sha256:"+hashlib.sha256(files[item["file"]]).hexdigest()
    if sum(len(files[i["file"]]) for i in report["outputs"]) > 64*1024**2:
        raise ValueError("native enriched routes exceed aggregate budget")
    report.update(native_export_report=original, original_bin_sha256=original["source_sha256"],
        original_bin_record_window=[start_record,stop_record],
        native_build_context_index=build_record, native_context_only_records=int(context_only),
        native_selected_observation_records=report["selected_records"],
        decoded_jsonl_basis="DERIVED_SELECTED_RECORDS_PLUS_BUILD_CONTEXT_NOT_COMPLETE_BIN_EXPORT",
        surface_depth=report["surface_depth"] if relation is not None else "NOT_EVALUATED_NATIVE_ANCHOR_DECLARATION_NOT_SUPPORTED",
        native_source_retention="retain original BIN, selected decoded JSONL, report and output CSVs")
    if relation is not None:
        report.update(native_anchor_record_index=anchor_index,
            native_anchor_byte_offset=records[indices.index(anchor_index)]["source_offset"],
            native_anchor_context_only_records=int(not start_record <= anchor_index < stop_record),
            native_context_only_records=int(context_only)+int(not start_record <= anchor_index < stop_record),
            original_bin_anchor_sha256="sha256:"+hashlib.sha256(surface_anchor).hexdigest(),
            derived_jsonl_anchor_sha256="sha256:"+hashlib.sha256(decoded_anchor).hexdigest())
        files["original-bin-surface-anchor.json"] = surface_anchor
    return files, report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input",type=Path);parser.add_argument("output",type=Path)
    for name in ("start-record","stop-record","build-record"):
        parser.add_argument("--"+name,type=int,required=True)
    parser.add_argument("--source-pin",required=True)
    parser.add_argument("--session-id",required=True)
    parser.add_argument("--single-boot-session",action="store_true")
    parser.add_argument("--mavlog-reader",type=Path,required=True)
    parser.add_argument("--qualify-session-domain",action="store_true")
    parser.add_argument("--surface-anchor",type=Path)
    args=parser.parse_args()
    try:
        anchor = None
        if args.surface_anchor is not None:
            with args.surface_anchor.open("rb") as source:
                anchor = source.read(65537)
        files,report=convert(args.input,start_record=args.start_record,stop_record=args.stop_record,
            build_record=args.build_record,source_pin=args.source_pin,session_id=args.session_id,
            single_boot_session=args.single_boot_session,mavlog_reader=args.mavlog_reader,
            qualify_session_domain=args.qualify_session_domain,surface_anchor=anchor)
        args.output.mkdir()
        for name,data in files.items(): (args.output/name).write_bytes(data)
        (args.output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False)+"\n")
    except (ValueError,OSError,TypeError,KeyError):
        parser.exit(2,"saved Sub BIN interval refused; check source, FMT, framing and bounds\n")


if __name__ == "__main__":
    main()
