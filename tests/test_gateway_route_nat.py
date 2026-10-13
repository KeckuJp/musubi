"""Authored saved declarations, never a live network configuration."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_gateway_route_nat import convert

ROOT = Path(__file__).resolve().parents[1]


class GatewayRouteNatTests(unittest.TestCase):
    def test_empty_exports_use_explicit_common_empty_policy(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
        self.assertTrue(reader, "reader-equipped stage required")
        metadata = b'{"nftables":[{"metainfo":{"version":"1.1.1","json_schema_version":1}}]}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.csv"
            for source_format, raw, count in (("route-ipv4", b"[]", 0), ("nftables", metadata, 1)):
                text, report = convert(raw, source_format, 0, "empty-declaration")
                self.assertEqual(report["source_records"], count)
                self.assertEqual(report["output_records"], 0)
                path.write_text(text)
                command = [reader, str(ROOT / "profiles/declared/gateway-route-nat/profile.toml"),
                           str(path), "--allow-equal-time"]
                self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
                result = subprocess.run(command + ["--allow-empty-rows"], capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(result.stdout)["observations"], [])
                # Opt-in accepts absence of rows, not an invalid header.
                path.write_text("wrong_header\n")
                self.assertNotEqual(subprocess.run(command + ["--allow-empty-rows"], capture_output=True).returncode, 0)
        for raw in (b"", b"{}", b"null"):
            with self.assertRaises((ValueError, TypeError)):
                convert(raw, "route-ipv4", 0, "empty-declaration")

    def test_routes_and_nat_reach_common(self):
        routes = [{"dst": "default", "gateway": "192.0.2.1", "dev": "eth0",
                   "table": "main", "type": "unicast", "metric": 100},
                  {"dst": "198.51.100.0/24", "type": "blackhole"}]
        rules = {"nftables": [{"metainfo": {"version": "1.1.1", "json_schema_version": 1}},
            {"chain": {"family": "ip", "table": "nat", "name": "post",
                       "type": "nat", "hook": "postrouting", "prio": 100}},
            {"rule": {"family": "ip", "table": "nat", "chain": "post", "handle": 4,
                      "expr": [{"match": {"op": "==", "left": {"meta": {"key": "oifname"}},
                                           "right": "eth0"}}, {"masquerade": None}]}}]}
        for kind, source, expected in (("route-ipv4", routes, 2), ("nftables", rules, 3)):
            output, report = convert(json.dumps(source).encode(), kind, 123, "capture-A")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), expected)
            self.assertEqual(report["withheld_records"], 0)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "input.csv"
                path.write_text(output)
                reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
                self.assertTrue(reader, "reader-equipped stage required")
                result = subprocess.run([reader, str(ROOT / "profiles/declared/gateway-route-nat/profile.toml"),
                                         str(path), "--allow-equal-time"], check=True, capture_output=True, text=True)
                observations = json.loads(result.stdout)["observations"]
                self.assertEqual(len(observations), expected)
            self.assertTrue(all(row["record_time_us"] == "123" for row in rows))
        self.assertEqual(rows[-1]["nat_action"], "masquerade")
        self.assertEqual(rows[-2]["match_subject"], "meta.oifname")

    def test_second_input_and_whole_rule_withholding(self):
        source = {"nftables": [{"metainfo": {"version": "1.1.1", "json_schema_version": 1}},
            {"rule": {"family": "ip", "table": "nat", "chain": "pre", "expr": [
                {"match": {"left": {"payload": {"protocol": "tcp", "field": "dport"}}, "op": "==", "right": 443}},
                {"dnat": {"addr": "198.51.100.4", "port": 8443}}]}},
            {"rule": {"family": "ip", "table": "nat", "chain": "pre", "expr": [
                {"lookup": {"set": "dynamic"}}, {"dnat": {"addr": "198.51.100.5"}}]}}]}
        text, report = convert(json.dumps(source).encode(), "nftables", 0, "B")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(rows[1]["nat_address"], "198.51.100.4")
        self.assertEqual(rows[1]["nat_port"], "8443")
        self.assertEqual(rows[-1]["disposition"], "WITHHELD_UNSUPPORTED_RULE")
        self.assertEqual(rows[-1]["nat_action"], "")
        self.assertEqual(report["source_records"], 3)
        self.assertEqual(report["withheld_records"], 1)
        self.assertEqual(report["metadata_records"], 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text(text)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_CSV_READER"],
                str(ROOT / "profiles/declared/gateway-route-nat/profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True, text=True)
            self.assertEqual(len(json.loads(result.stdout)["observations"]), 3)

    def test_invalid_and_unsupported_route(self):
        for raw in (b'[{"dst":"999.1.1.1"}]', b'[{"dst":"default","metric":true}]',
                    b'[{"dst":"default","dst":"default"}]'):
            with self.assertRaises(ValueError):
                convert(raw, "route-ipv4", 0, "X")
        text, report = convert(b'[{"dst":"default","nexthops":[]}]', "route-ipv4", 0, "X")
        self.assertIn("WITHHELD_UNSUPPORTED_ROUTE", text)
        self.assertEqual(report["withheld_records"], 1)

    def test_cli_original_bytes_empty_and_writer_gate(self):
        raw = b'[ {"dst":"203.0.113.0/24","dev":"lan0","metric":0} ]\n'
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source.json"
            source.write_bytes(raw)
            subprocess.run(["python3", str(ROOT / "scripts/convert_gateway_route_nat.py"),
                str(source), str(base / "out"), "--source-format", "route-ipv4",
                "--capture-time-us", "0", "--snapshot-id", "declared"], check=True, capture_output=True)
            self.assertEqual((base / "out/source.json").read_bytes(), raw)
            rows = list(csv.DictReader(io.StringIO((base / "out/observations.csv").read_text())))
            self.assertEqual(rows[0]["route_metric"], "0")
            self.assertEqual(rows[0]["route_gateway"], "")
        text, report = convert(b'[]', "route-ipv4", 0, "empty")
        self.assertEqual(report["output_records"], 0)
        self.assertEqual(list(csv.DictReader(io.StringIO(text))), [])
        with self.assertRaises(ValueError):
            convert(b'{"nftables":[{"metainfo":{"version":"1.2.0","json_schema_version":1}}]}',
                    "nftables", 0, "bad-writer")

    def test_translation_variants_and_invalid_port(self):
        meta = {"metainfo": {"version": "1.1.1", "json_schema_version": 1}}
        for action, body in (("snat", {"addr": "192.0.2.8", "family": "ip", "flags": "persistent"}),
                             ("redirect", {"port": 8080}), ("masquerade", {})):
            rule = {"rule": {"family": "ip", "table": "nat", "chain": "custom", "expr": [{action: body}]}}
            text, report = convert(json.dumps({"nftables": [meta, rule]}).encode(), "nftables", 1, "C")
            self.assertEqual(list(csv.DictReader(io.StringIO(text)))[0]["nat_action"], action)
            self.assertEqual(report["interpreted_records"], 1)
        rule["rule"]["expr"] = [{"dnat": {"port": True}}]
        with self.assertRaises(ValueError):
            convert(json.dumps({"nftables": [meta, rule]}).encode(), "nftables", 1, "C")
