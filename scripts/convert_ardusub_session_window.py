"""Bounded saved decoded Sub JSONL plus explicit same-original build context."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_ardusub_session as session
from scripts.convert_vda_load_report import read_jsonl_window

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/ardupilot-mode/imu-unclassified-profile.toml"


def convert(path, *, start_record, stop_record, build_record, source_pin, session_id,
            single_boot_session, mavlog_reader, surface_anchor=None, qualify_session_domain=False):
    if type(qualify_session_domain) is not bool:
        raise ValueError("session domain option must be boolean")
    if source_pin not in session.SOURCE_PINS:
        raise ValueError("unsupported fixed Sub source pin")
    version = session.SOURCE_PINS[source_pin]
    if type(build_record) is not int or not 0 <= build_record < stop_record:
        raise ValueError("explicit preceding or selected build record required")
    selected, offsets, count, _, digest = read_jsonl_window(path,
        start_record=start_record, stop_record=stop_record)
    window = "".join(selected).encode()
    refs = list(zip(range(start_record, stop_record), offsets))
    context_only = build_record < start_record
    if context_only:
        context, context_offsets, context_count, _, context_digest = read_jsonl_window(path,
            start_record=build_record, stop_record=build_record + 1,
            expected_size=Path(path).stat().st_size, expected_sha256=digest)
        if context_count != count or context_digest != digest:
            raise ValueError("context and selected snapshots differ")
        selected = context + selected
        refs = [(build_record, context_offsets[0])] + refs
        context_index = 0
    else:
        context_index = build_record - start_record
    original_relation = None
    anchor_context = False
    if surface_anchor is not None:
        if not isinstance(surface_anchor, bytes) or not 0 < len(surface_anchor) <= 65536:
            raise ValueError("bounded original anchor bytes required")
        original_relation = json.loads(surface_anchor, object_pairs_hook=session.vertical.unique_object,
            parse_constant=session.vertical.reject_constant)
        expected = session.vertical.ANCHOR_FIELDS - {"source_report_sha256"} | {"original_jsonl_sha256"}
        if (not isinstance(original_relation, dict) or set(original_relation) != expected
                or original_relation["schema"] != "ardusub-jsonl-declared-surface/v1"
                or original_relation["original_jsonl_sha256"] != "sha256:" + digest
                or original_relation["session_id"] != session_id):
            raise ValueError("original anchor source/schema/session mismatch")
        session.vertical.integer(original_relation["anchor_record_index"], "anchor index", 2**63-1)
        anchor_global = original_relation["anchor_record_index"]
        anchor_context = not start_record <= anchor_global < stop_record
        if anchor_context:
            extra, extra_offsets, extra_count, _, extra_digest = read_jsonl_window(path,
                start_record=anchor_global, stop_record=anchor_global+1,
                expected_size=Path(path).stat().st_size, expected_sha256=digest)
            if extra_count != count or extra_digest != digest:
                raise ValueError("anchor and selected snapshots differ")
            pairs = sorted(zip(refs + [(anchor_global, extra_offsets[0])], selected + extra))
            refs, selected = [p[0] for p in pairs], [p[1] for p in pairs]
            context_index = [r[0] for r in refs].index(build_record)
    records = [json.loads(line, object_pairs_hook=session.vertical.unique_object,
                          parse_constant=session.vertical.reject_constant) for line in selected]
    context = records[context_index]
    if (not isinstance(context, dict) or context.get("meta", {}).get("type") != "MSG"
            or context.get("data", {}).get("Message") != f"ArduSub V{version} ({source_pin[:8]})"):
        raise ValueError("selected context must be the fixed build MSG")
    native = "".join(selected).encode()
    if len(native) > session.LIMIT:
        raise ValueError("selected plus context exceeds reader bound")
    with tempfile.TemporaryDirectory() as directory:
        input_path = Path(directory) / "selected.jsonl"
        report_path = Path(directory) / "reader.json"
        input_path.write_bytes(native)
        with report_path.open("wb") as output:
            process = subprocess.run([str(mavlog_reader), str(PROFILE), str(input_path)],
                stdout=output, stderr=subprocess.PIPE)
        if process.returncode:
            raise ValueError("existing saved JSONL reader refused selected records")
        with report_path.open("rb") as source:
            raw = source.read(session.LIMIT + 1)
    if len(raw) > session.LIMIT:
        raise ValueError("expanded reader report too large; choose smaller window")
    document = json.loads(raw)
    if document.get("source_records") != records:
        raise ValueError("reader source record correspondence changed")
    derived_anchor = None
    if original_relation is not None:
        if not any(r.get("meta", {}).get("type") == "CTUN" and start_record <= ref[0] < stop_record
                   for r, ref in zip(records, refs)):
            raise ValueError("anchor context cannot supply a missing window CTUN")
        relation = dict(original_relation)
        relation.pop("original_jsonl_sha256")
        relation.update(schema="ardusub-declared-surface/v1",
            source_report_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            anchor_record_index=[r[0] for r in refs].index(anchor_global))
        derived_anchor = json.dumps(relation, separators=(",", ":"), allow_nan=False).encode()
    files, report = session.convert(raw, source_pin=source_pin, session_id=session_id,
        single_boot_session=single_boot_session, surface_anchor=derived_anchor,
        qualify_session_domain=qualify_session_domain)
    window_digest = "sha256:" + hashlib.sha256(window).hexdigest()
    for item in report["outputs"]:
        reader = csv.DictReader(io.StringIO(files[item["file"]].decode()))
        rows = list(reader)
        if anchor_context:
            rows = [row for row in rows if refs[int(row["session_source_record_index"])][0] != anchor_global]
        fields = ["original_jsonl_sha256", "original_jsonl_record_index", "original_jsonl_byte_offset",
                  "selected_window_sha256", "build_context_original_index", "window_association_basis"]
        if original_relation is not None:
            fields += ["original_anchor_sha256", "anchor_original_record_index"]
        for row in rows:
            local = int(row["session_source_record_index"])
            index, offset = refs[local]
            if not start_record <= index < stop_record:
                raise ValueError("context unexpectedly produced an observation")
            row.update(original_jsonl_sha256="sha256:" + digest,
                original_jsonl_record_index=index, original_jsonl_byte_offset=offset,
                selected_window_sha256=window_digest, build_context_original_index=build_record,
                window_association_basis="SAME_HASHED_ORIGINAL_CALLER_BOOT_DECLARATION_OUTSIDE_SEMANTICS_NOT_EVALUATED")
            if original_relation is not None:
                row.update(original_anchor_sha256="sha256:" + hashlib.sha256(surface_anchor).hexdigest(),
                           anchor_original_record_index=anchor_global)
        files[item["file"]] = session.table(rows, reader.fieldnames + fields).encode()
        if len(files[item["file"]]) > session.LIMIT:
            raise ValueError("enriched route exceeds bound; choose smaller window")
        item["sha256"] = "sha256:" + hashlib.sha256(files[item["file"]]).hexdigest()
        if original_relation is not None:
            item["records"] = len(rows)
    if sum(map(len, files.values())) > 64 * 1024**2:
        raise ValueError("window bundle exceeds output budget")
    files.update({"source-report.json": raw, "selected-with-context.jsonl": native})
    report.update(original_jsonl_sha256="sha256:" + digest, original_records=count,
        original_record_window=[start_record, stop_record], window_records=stop_record-start_record,
        outside_window_records=count-(stop_record-start_record),
        context_only_records=int(context_only), build_context_original_index=build_record,
        selected_window_sha256=window_digest,
        selected_with_context_sha256="sha256:" + hashlib.sha256(native).hexdigest(),
        window_unselected_records=report["not_selected_records"]-int(context_only),
        outside_meaning="NOT_EVALUATED; build contradictions outside selected/context are not checked",
        surface_depth="NOT_EVALUATED; this window entry does not relocate original surface anchor declarations",
        source_retention="retain original JSONL and both selected/report derivatives; original SHA is not derivative SHA")
    if original_relation is not None:
        report["validated_with_context_selected_records"] = report["selected_records"]
        report["selected_records"] -= int(anchor_context)
        report["selected_counts"]["CTUN"] -= int(anchor_context)
        report.update(context_only_records=int(context_only)+int(anchor_context),
            anchor_context_only_records=int(anchor_context),
            surface_depth="CONDITIONAL_CALLER_SURFACE_ORIGIN_RESET_TRUTH_NOT_VERIFIED",
            anchor_original_record_index=anchor_global,
            anchor_original_byte_offset=refs[relation["anchor_record_index"]][1],
            anchor_reader_report_record_index=relation["anchor_record_index"],
            original_anchor_sha256="sha256:" + hashlib.sha256(surface_anchor).hexdigest(),
            derived_anchor_sha256="sha256:" + hashlib.sha256(derived_anchor).hexdigest(),
            vertical_report_scope="VALIDATED_READER_REPORT_INCLUDING_CONTEXT; output counts exclude context-only anchor")
        files.update({"original-surface-anchor.json": surface_anchor,
                      "derived-surface-anchor.json": derived_anchor})
    return files, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    for name in ("start-record", "stop-record", "build-record"):
        parser.add_argument("--" + name, required=True, type=int)
    parser.add_argument("--source-pin", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--single-boot-session", action="store_true")
    parser.add_argument("--mavlog-reader", required=True, type=Path)
    parser.add_argument("--surface-anchor", type=Path)
    parser.add_argument("--qualify-session-domain", action="store_true")
    args = parser.parse_args()
    try:
        anchor = None
        if args.surface_anchor is not None:
            with args.surface_anchor.open("rb") as source:
                anchor = source.read(65537)
        files, report = convert(args.input, start_record=args.start_record, stop_record=args.stop_record,
            build_record=args.build_record, source_pin=args.source_pin, session_id=args.session_id,
            single_boot_session=args.single_boot_session, mavlog_reader=args.mavlog_reader, surface_anchor=anchor,
            qualify_session_domain=args.qualify_session_domain)
        args.output.mkdir()
        for name, data in files.items():
            (args.output / name).write_bytes(data)
        (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError, TypeError, KeyError):
        parser.exit(2, "saved Sub window refused; check context, window and source bounds\n")


if __name__ == "__main__":
    main()
