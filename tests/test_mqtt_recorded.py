"""Authored saved envelopes; no broker or physical recording claim."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_mqtt_recorded import convert
from tests.test_electrical_schema_reuse import vda, ros

ROOT = Path(__file__).resolve().parents[1]


def envelope(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return dict(tst="2026-01-01T01:00:01.123456+0100", topic="example/state", qos=1,
                mid=42, retain=1, payloadlen=len(text.encode()), payload=text, future=["保持"])


class MqttRecordedTests(unittest.TestCase):




    def test_rejects_wrong_envelope_and_payload_without_partial_success(self):
        base = envelope(vda("2.1.0"))
        for patch in [dict(topic="other"), dict(payloadlen=1), dict(payload={}), dict(qos=True),
                      dict(retain=2), dict(mid=0), dict(tst="2026-01-01T00:00:00Z"),
                      dict(tst="2026-02-30T00:00:00.000000+0000")]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                convert(json.dumps(dict(base, **patch)), "example/state", "vda-state-2.1")
        good = json.dumps(base)
        for text, topic, fmt in [(good, "#", "vda-state-2.1"), (good, "example/state", "vda-state-3.0"),
                (good, "example/state", "vda-state-2.0"),
                (good + "\n\n", "example/state", "vda-state-2.1"),
                (good[:-1] + ',"qos":1}', "example/state", "vda-state-2.1"),
                (good + "\n" + json.dumps(dict(base, tst="2025-01-01T00:00:00.000000+0000")),
                 "example/state", "vda-state-2.1")]:
            with self.assertRaises(ValueError):
                convert(text, topic, fmt)


if __name__ == "__main__":
    unittest.main()
