#!/usr/bin/env python3
"""Bind explicit CAP references only within a supplied finite set of saved messages."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

try:
    from . import convert_cap_alert as cap
except ImportError:
    import convert_cap_alert as cap

MAX_DOCUMENTS = 64
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_REFERENCES = 4096
MAX_RELATION_ROWS = 4096
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
FIELDS = ["record_time_us", "source_document_index", "source_sha256", "source_sender_hex",
          "source_identifier_hex", "source_sent_text_hex", "source_msg_type", "reference_index",
          "reference_token_hex", "reference_sender_hex", "reference_identifier_hex",
          "reference_sent_text_hex", "declared_relation_kind", "relation_disposition",
          "target_candidate_count", "target_candidate_index", "target_document_index",
          "target_source_sha256", "target_relation_basis"]


def report_text(report):
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def first_row(text):
    # The unchanged CAP converter may emit a large references field. Parse its canonical CSV,
    # not the original XML again, and restore the process-wide Python csv setting immediately.
    previous = csv.field_size_limit(cap.MAX_OUTPUT)
    try:
        return next(csv.DictReader(io.StringIO(text)))
    finally:
        csv.field_size_limit(previous)


def reference_key(token):
    parts = token.split(",")
    if len(parts) != 3 or any(not cap.CAP_IDENTIFIER.fullmatch(p) for p in parts[:2]):
        return None
    try:
        cap.cap_time_us(parts[2], "reference sent")
    except ValueError:
        return None
    # The validated instant is deliberately not the key. Offset/fraction spelling stays literal.
    return tuple(parts)


def convert(documents):
    if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_DOCUMENTS:
        raise ValueError("CAP supplied document count bound exceeded")
    if any(not isinstance(data, bytes) for data in documents):
        raise ValueError("CAP documents must be saved bytes")
    if sum(map(len, documents)) > MAX_INPUT_BYTES:
        raise ValueError("CAP aggregate input bound exceeded")
    outputs, firsts, sources, identities = [], [], [], {}
    written = 0
    for index, data in enumerate(documents):
        output, source_report = cap.convert(data, output_budget=MAX_OUTPUT_BYTES-written)
        written += len(output.encode("utf-8"))
        row = first_row(output)
        key = tuple(row[name] for name in ("alert_sender", "alert_identifier", "alert_sent_text"))
        identities.setdefault(key, []).append(index)
        firsts.append(row)
        outputs.append(output)
        sources.append(dict(document_index=index, original_file=f"source-{index:03}.xml",
                            observations_file=f"source-{index:03}.csv", cap_report=source_report))
    report = dict(schema="cap-supplied-relations/v1", source_documents=len(documents),
                  source_observation_rows=sum(s["cap_report"]["rows"] for s in sources),
                  reference_tokens=0, relation_rows=0, matched_references=0, missing_references=0,
                  ambiguous_references=0, malformed_references=0, no_reference_documents=0,
                  documents=sources, match_basis="EXACT_DECLARED_SENDER_IDENTIFIER_LITERAL_SENT_NOT_AUTHENTICATION",
                  serialization="STABLE_REPORTED_SENT_US_SORT_NOT_PHYSICAL_OR_CAUSAL_ORDER",
                  automatic_state_changes="NONE")
    rows = []

    def append(row):
        if len(rows) >= MAX_RELATION_ROWS:
            raise ValueError("CAP relation row bound exceeded")
        rows.append(row)

    for index, source in enumerate(firsts):
        kind = source["alert_msg_type"]
        base = dict(record_time_us=int(source["record_time_us"]), source_document_index=index,
                    source_sha256="sha256:"+source["source_sha256"],
                    source_sender_hex=cap.hexed(source["alert_sender"]),
                    source_identifier_hex=cap.hexed(source["alert_identifier"]),
                    source_sent_text_hex=cap.hexed(source["alert_sent_text"]), source_msg_type=kind,
                    declared_relation_kind=(kind.upper()+"_DECLARED_REFERENCE" if kind in ("Update", "Cancel")
                                            else "REFERENCE_NOT_UPDATE_OR_CANCEL"))
        encoded = source["alert_references_hex"]
        tokens = bytes.fromhex(encoded[4:]).decode("utf-8").split() if encoded else []
        report["reference_tokens"] += len(tokens)
        if report["reference_tokens"] > MAX_REFERENCES:
            raise ValueError("CAP reference count bound exceeded")
        if not tokens:
            report["no_reference_documents"] += 1
            append(dict(base, relation_disposition="NO_REFERENCES_DECLARED", target_candidate_count=0))
        for ref_index, token in enumerate(tokens):
            key = reference_key(token)
            reference = dict(base, reference_index=ref_index, reference_token_hex=cap.hexed(token))
            if key is None:
                report["malformed_references"] += 1
                append(dict(reference, relation_disposition="MALFORMED_REFERENCE_RETAINED",
                            target_candidate_count=0))
                continue
            reference.update(zip(("reference_sender_hex", "reference_identifier_hex", "reference_sent_text_hex"),
                                 map(cap.hexed, key)))
            targets = identities.get(key, [])
            count = len(targets)
            disposition = ("TARGET_MISSING" if not count else "MATCHED_DECLARED_IDENTITY" if count == 1
                           else "AMBIGUOUS_SUPPLIED_IDENTITY")
            report["missing_references" if not count else "matched_references" if count == 1
                   else "ambiguous_references"] += 1
            reference.update(relation_disposition=disposition, target_candidate_count=count)
            if not targets:
                append(reference)
            for candidate_index, target in enumerate(targets):
                append(dict(reference, target_candidate_index=candidate_index, target_document_index=target,
                            target_source_sha256="sha256:"+firsts[target]["source_sha256"],
                            target_relation_basis="SELF_REFERENCE_NOT_PREDECESSOR" if target == index
                            else "DECLARED_REFERENCE_ONLY_NOT_ACCEPTANCE_OR_STATE_CHANGE"))
    rows.sort(key=lambda row: row["record_time_us"])
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    # All relation text is fixed ASCII or UTF-8 hex, so character count equals bytes here.
    for row in rows:
        writer.writerow(row)
        if written + output.tell() > MAX_OUTPUT_BYTES:
            raise ValueError("CAP aggregate output bound exceeded")
    report["relation_rows"] = len(rows)
    report["relations_sha256"] = hashlib.sha256(output.getvalue().encode("utf-8")).hexdigest()
    if written + output.tell() + len(report_text(report).encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("CAP aggregate output including report bound exceeded")
    return outputs, output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("sources", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("CAP output directory must be new")
        if len(args.sources) > MAX_DOCUMENTS:
            raise ValueError("CAP supplied document count bound exceeded")
        documents, size = [], 0
        for path in args.sources:
            if not path.is_file():
                raise ValueError("CAP input must be a supplied regular file")
            with path.open("rb") as source:
                data = source.read(MAX_INPUT_BYTES-size+1)
            size += len(data)
            if size > MAX_INPUT_BYTES:
                raise ValueError("CAP aggregate input bound exceeded")
            documents.append(data)
        outputs, relations, report = convert(documents)
        args.output.mkdir(parents=True, exist_ok=False)
        for index, (data, text) in enumerate(zip(documents, outputs)):
            (args.output/f"source-{index:03}.xml").write_bytes(data)
            (args.output/f"source-{index:03}.csv").write_text(text, encoding="utf-8")
        (args.output/"relations.csv").write_text(relations, encoding="utf-8")
        (args.output/"report.json").write_text(report_text(report), encoding="utf-8")
        return 0
    except (ValueError, OSError) as exc:
        print(f"CAP supplied relation conversion rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
