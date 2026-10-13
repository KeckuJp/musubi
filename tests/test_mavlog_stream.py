"""Authored pipe proof: output must arrive while the stdin writer remains open.

Build both reader examples first. No recordings, device, socket or external decoder.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
BINARY = TARGET / "debug/examples/read_mavlog_stream"
FILE_READER = TARGET / "debug/examples/read_mavlog_json"
PROFILE = ROOT / "profiles/declared/arduplane-jsonl/profile.toml"


def row(t, label="authored"):
    return json.dumps({"meta": {"type": "GPS"}, "data": {
        "TimeUS": t, "Lat": 12.5, "Lng": -45.25, "Alt": -1.234,
        "Future": {"label": label},
    }}, ensure_ascii=False).encode() + b"\n"


class MavlogStreamPipe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (BINARY, FILE_READER):
            if not binary.is_file():
                raise AssertionError(f"build the reader example first: {binary.name}")

    def start(self):
        return subprocess.Popen([str(BINARY), str(PROFILE), "authored-pipe"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)

    def read_event(self, process, timeout=3):
        # Read one byte at a time, avoiding buffered readline hiding later output
        # from select(). A timeout fails the test while the producer is still open.
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            self.assertTrue(ready, "no event before EOF while producer remains open")
            part = os.read(process.stdout.fileno(), 1)
            self.assertTrue(part, "stream reader ended before a complete output event")
            chunks.extend(part)
        return json.loads(chunks)

    def stop(self, process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()

    def test_open_writer_first_record_then_fragmented_reuse_matches_file(self):
        process = self.start()
        try:
            first = row(9000, "一番目")
            process.stdin.write(first)
            first_event = self.read_event(process)
            self.assertIsNone(process.poll())
            self.assertFalse(process.stdin.closed)
            self.assertEqual(first_event["kind"], "observation")
            second = row(9000, "同時刻")
            split = second.index("同".encode()) + 1
            process.stdin.write(second[:split])
            self.assertFalse(select.select([process.stdout], [], [], 0.1)[0])
            process.stdin.write(second[split:])
            second_event = self.read_event(process)
            process.stdin.write(row(1000, "reset"))
            third_event = self.read_event(process)
            self.assertIsNone(process.poll())
            self.assertEqual([e["observation"]["t_boot_us"] for e in
                              (first_event, second_event, third_event)], [9000, 9000, 1000])
            for event in (first_event, second_event, third_event):
                self.assertEqual(event["source_id"], "authored-pipe")
                self.assertEqual(event["platform_domain"], "Unknown")
                self.assertEqual(event["observation"]["clock_basis"], "BootRelative")
                self.assertIsNone(event["observation"]["wall_ms"])
                self.assertEqual(event["arrival_clock_basis"], "untrusted_host_capture")
                self.assertIsInstance(event["arrival_unix_ms"], int)
                self.assertEqual(event["profile_units"]["GPS.Alt"], "m")
            process.stdin.close()
            summary = self.read_event(process)
            self.assertEqual(summary["kind"], "summary")
            self.assertEqual(summary["counts"]["observations"], 3)
            self.assertEqual(summary["counts"]["frames"], 3)
            self.assertEqual(process.wait(timeout=3), 0)
            with tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory) / "authored.jsonl"
                fixture.write_bytes(first + second + row(1000, "reset"))
                result = subprocess.run([str(FILE_READER), str(PROFILE), str(fixture)],
                                        capture_output=True, check=True)
            existing = json.loads(result.stdout)
            for event, expected in zip((first_event, second_event, third_event),
                                       existing["observations"]):
                for key, value in expected.items():
                    self.assertEqual(event["observation"][key], value)
        finally:
            self.stop(process)

    def test_oversized_then_valid_and_incomplete_eof_are_accounted(self):
        raw = b"x" * 65537 + b"\n" + row(7) + b'{"meta":'
        result = subprocess.run([str(BINARY), str(PROFILE), "failure-reuse"],
                                input=raw, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([e["kind"] for e in events],
                         ["oversized", "observation", "incomplete_eof", "summary"])
        self.assertTrue(events[0]["source_bytes_truncated"])
        self.assertEqual(events[0]["frame_bytes"], 65537)
        self.assertEqual(len(bytes.fromhex(events[0]["source_bytes_hex"])), 65536)
        self.assertFalse(events[2]["terminated_by_lf"])
        counts = events[-1]["counts"]
        self.assertEqual(counts["bytes_received"], len(raw))
        self.assertEqual((counts["frames"], counts["observations"],
                          counts["oversized_frames"], counts["incomplete_frames"]), (3, 1, 1, 1))

    def test_other_messages_invalid_json_and_duplicate_keys_stay_distinct(self):
        raw = (b'{"meta":{"type":"MSG"},"data":{"Message":"ArduPlane authored"}}\n'
               b'{"meta":{"type":"GPS"},"data":{"Lat":1}}\n'
               b'broken\n'
               b'{"meta":{"type":"GPS"},"data":{"TimeUS":1,"TimeUS":2}}\n'
               + row(9))
        result = subprocess.run([str(BINARY), str(PROFILE), "scoped-reuse"],
                                input=raw, capture_output=True, timeout=10)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([e["kind"] for e in events],
                         ["unsupported", "unsupported", "invalid", "invalid",
                          "observation", "summary"])
        counts = events[-1]["counts"]
        self.assertEqual((counts["frames"], counts["observations"],
                          counts["unsupported_frames"], counts["invalid_frames"]), (5, 1, 2, 2))


if __name__ == "__main__":
    unittest.main()
