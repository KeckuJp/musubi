#!/usr/bin/env python3
"""Read saved IPv4 route / nftables declarations; never execute networking commands."""
import argparse
import csv
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.convert_gateway_link_state import strict_json

INPUT_LIMIT = 64 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
FIELDS = ["record_time_us", "snapshot_id_hex", "source_sha256", "source_index",
          "expression_index", "record_kind", "disposition", "reason", "raw_json_hex",
          "route_destination", "route_type", "route_table", "route_gateway", "route_dev_hex",
          "route_metric", "route_protocol", "route_scope", "route_prefsrc",
          "family", "table_hex", "chain_hex", "rule_handle", "chain_type", "chain_hook",
          "chain_priority", "chain_policy", "match_subject", "match_operator", "match_value_hex",
          "nat_action", "nat_address", "nat_port", "nat_flags", "counter_packets", "counter_bytes"]
ROUTE_KEYS = {"dst", "type", "table", "gateway", "dev", "metric", "protocol", "scope", "prefsrc"}


class Unsupported(ValueError):
    pass


def string(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid declared string")
    return value


def integer(value, maximum=2**63 - 1, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid declared integer")
    return value


def hextext(value):
    return "hex:" + string(value).encode().hex()


def rawhex(value):
    return "hex:" + json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode().hex()


def address(value):
    return str(ipaddress.IPv4Address(string(value)))


def selected_keys(value, keys):
    if not isinstance(value, dict):
        raise ValueError("expected object")
    if value.keys() - keys:
        raise Unsupported("UNSUPPORTED_KEYS")


def route_row(value):
    selected_keys(value, ROUTE_KEYS)
    destination = string(value["dst"])
    destination = "0.0.0.0/0" if destination == "default" else str(ipaddress.IPv4Network(destination, strict=True))
    row = {"record_kind": "ROUTE", "route_destination": destination}
    for key in ("type", "table", "protocol", "scope"):
        if key in value:
            row["route_" + key] = string(value[key])
    for key in ("gateway", "prefsrc"):
        if key in value:
            row["route_" + key] = address(value[key])
    if "dev" in value:
        row["route_dev_hex"] = hextext(value["dev"])
    if "metric" in value:
        row["route_metric"] = integer(value["metric"], 2**32 - 1)
    return row


def match_row(value):
    selected_keys(value, {"left", "right", "op"})
    if value["op"] not in ("==", "!="):
        raise Unsupported("UNSUPPORTED_MATCH_OPERATOR")
    left, right = value["left"], value["right"]
    if not isinstance(left, dict) or len(left) != 1:
        raise Unsupported("UNSUPPORTED_MATCH_LEFT")
    if "meta" in left:
        selected_keys(left["meta"], {"key"})
        key = left["meta"]["key"]
        if key not in ("iifname", "oifname", "l4proto"):
            raise Unsupported("UNSUPPORTED_META")
        string(right)
        subject = "meta." + key
    elif "payload" in left:
        selected_keys(left["payload"], {"protocol", "field"})
        protocol, field = left["payload"]["protocol"], left["payload"]["field"]
        if protocol == "ip" and field in ("saddr", "daddr"):
            right = address(right)
        elif protocol in ("tcp", "udp") and field in ("sport", "dport"):
            right = str(integer(right, 65535))
        else:
            raise Unsupported("UNSUPPORTED_PAYLOAD")
        subject = protocol + "." + field
    else:
        raise Unsupported("UNSUPPORTED_MATCH_LEFT")
    return {"record_kind": "MATCH_DECLARATION", "match_subject": subject,
            "match_operator": value["op"], "match_value_hex": hextext(right)}


def statement(value):
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("statement requires one key")
    kind, body = next(iter(value.items()))
    if kind == "match":
        return match_row(body)
    if kind == "counter":
        selected_keys(body, {"packets", "bytes"})
        return {"record_kind": "COUNTER_DECLARATION", **{
            "counter_" + key: integer(body[key]) for key in ("packets", "bytes") if key in body}}
    if kind not in ("snat", "dnat", "masquerade", "redirect"):
        raise Unsupported("UNSUPPORTED_STATEMENT")
    # Null is the documented representation of a statement with no properties.
    body = {} if body is None else body
    allowed = {"port", "flags"} | ({"addr", "family"} if kind in ("snat", "dnat") else set())
    selected_keys(body, allowed)
    if "family" in body and body["family"] != "ip":
        raise Unsupported("UNSUPPORTED_NAT_FAMILY")
    row = {"record_kind": "NAT_DECLARATION", "nat_action": kind}
    if "addr" in body:
        if not isinstance(body["addr"], str):
            raise Unsupported("NONLITERAL_NAT_ADDRESS")
        row["nat_address"] = address(body["addr"])
    if "port" in body:
        if isinstance(body["port"], (dict, list)):
            raise Unsupported("NONLITERAL_NAT_PORT")
        row["nat_port"] = integer(body["port"], 65535)
    if "flags" in body:
        flags = body["flags"]
        flags = [flags] if isinstance(flags, str) else flags
        if not isinstance(flags, list) or any(flag not in ("random", "fully-random", "persistent") for flag in flags):
            raise Unsupported("UNSUPPORTED_NAT_FLAGS")
        row["nat_flags"] = "|".join(flags)
    return row


def nft_rows(entry):
    if not isinstance(entry, dict) or len(entry) != 1:
        raise ValueError("ruleset entry requires one key")
    kind, value = next(iter(entry.items()))
    if kind not in ("chain", "rule"):
        raise Unsupported("UNSELECTED_RULESET_OBJECT")
    if not isinstance(value, dict):
        raise ValueError("ruleset object required")
    if value.get("family") != "ip":
        raise Unsupported("UNSUPPORTED_FAMILY")
    common = {"family": "ip", "table_hex": hextext(value["table"]),
              "chain_hex": hextext(value["name" if kind == "chain" else "chain"])}
    if kind == "chain":
        selected_keys(value, {"family", "table", "name", "handle", "type", "hook", "prio", "policy"})
        row = {**common, "record_kind": "CHAIN_DECLARATION"}
        for key in ("type", "hook", "policy"):
            if key in value:
                row["chain_" + key] = string(value[key])
        if "prio" in value:
            row["chain_priority"] = integer(value["prio"], 2**31 - 1, -(2**31))
        return [row]
    selected_keys(value, {"family", "table", "chain", "handle", "expr", "comment"})
    if "handle" in value:
        common["rule_handle"] = integer(value["handle"])
    expressions = value["expr"]
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= 1024:
        raise ValueError("invalid statement count")
    # Construct the whole rule before emitting anything. Unsupported predicates cannot leave
    # behind a misleading unconditional translation declaration.
    return [{**common, **statement(expr), "expression_index": index}
            for index, expr in enumerate(expressions)]


def convert(raw, source_format, capture_time_us, snapshot_id):
    integer(capture_time_us)
    hextext(snapshot_id)
    if len(raw) > INPUT_LIMIT:
        raise ValueError("input exceeds bound")
    document = strict_json(raw.decode("utf-8"))
    if source_format == "route-ipv4":
        entries = document
    elif source_format == "nftables":
        if not isinstance(document, dict) or set(document) != {"nftables"}:
            raise ValueError("saved ruleset required")
        entries = document["nftables"]
    else:
        raise ValueError("unsupported source format")
    if not isinstance(entries, list) or len(entries) > 200000:
        raise ValueError("invalid record count")
    if source_format == "nftables":
        metadata = [entry["metainfo"] for entry in entries if isinstance(entry, dict) and "metainfo" in entry]
        if len(metadata) != 1 or not isinstance(metadata[0], dict) or metadata[0].get("version") != "1.1.1" or type(metadata[0].get("json_schema_version")) is not int or metadata[0]["json_schema_version"] != 1:
            raise ValueError("nftables writer/version gate failed")
    digest = hashlib.sha256(raw).hexdigest()
    report = {"source_sha256": digest, "source_format": source_format, "source_records": len(entries),
              "interpreted_records": 0, "withheld_records": 0, "metadata_records": 0,
              "output_records": 0, "clock_basis": "CALLER_CAPTURE_UNKNOWN",
              "claim": "SAVED_DECLARATIONS_NOT_PACKET_EVALUATION_OR_AUTHENTICATED_DEVICE_STATE"}
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    output_bytes = len(out.getvalue().encode("utf-8"))
    for index, entry in enumerate(entries):
        if source_format == "nftables" and isinstance(entry, dict) and set(entry) == {"metainfo"}:
            report["metadata_records"] += 1
            continue
        common = {"record_time_us": capture_time_us, "snapshot_id_hex": hextext(snapshot_id),
                  "source_sha256": digest, "source_index": index, "raw_json_hex": rawhex(entry)}
        try:
            rows = [route_row(entry)] if source_format == "route-ipv4" else nft_rows(entry)
            report["interpreted_records"] += 1
            disposition = "DECLARED_NOT_EVALUATED"
        except Unsupported as exc:
            report["withheld_records"] += 1
            disposition = "WITHHELD_UNSUPPORTED_ROUTE" if source_format == "route-ipv4" else "WITHHELD_UNSUPPORTED_RULE"
            rows = [{"record_kind": "WITHHELD", "reason": str(exc)}]
        for row in rows:
            line = io.StringIO()
            csv.DictWriter(line, fieldnames=FIELDS).writerow({**common, "disposition": disposition, **row})
            serialized = line.getvalue()
            output_bytes += len(serialized.encode("utf-8"))
            if output_bytes > OUTPUT_LIMIT:
                raise ValueError("output exceeds bound")
            out.write(serialized)
            report["output_records"] += 1
    return out.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-format", required=True, choices=("route-ipv4", "nftables"))
    parser.add_argument("--capture-time-us", type=int, required=True)
    parser.add_argument("--snapshot-id", required=True)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(INPUT_LIMIT + 1)
        text, report = convert(raw, args.source_format, args.capture_time_us, args.snapshot_id)
        args.output.mkdir()
        (args.output / "source.json").write_bytes(raw)
        (args.output / "observations.csv").write_text(text, encoding="utf-8")
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, OverflowError):
        parser.exit(2, "Saved gateway declaration conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
