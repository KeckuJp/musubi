"""Shared authored native capture and actual-common assertions, not a decoder."""
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def native(frames):
    return "".join(f"(1700000000.000001) can0 {identifier}#{data}\r\n"
                   for _, identifier, data in (line.split(",") for line in frames.splitlines()))


def common(test, folder, output, profile=None):
    reader = os.environ.get("MUSUBI_TELEMETRY_READER")
    test.assertTrue(reader, "postbuild common reader required")
    path = folder / "selected.csv"
    path.write_text(output)
    profile = profile or ROOT / "profiles/declared/dronecan-recorded/profile.toml"
    run = subprocess.run([reader, str(profile),
                          str(path), "--allow-equal-time"], capture_output=True, text=True)
    test.assertEqual(run.returncode, 0, run.stderr)
    return json.loads(run.stdout)
