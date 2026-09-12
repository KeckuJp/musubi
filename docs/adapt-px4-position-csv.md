# Adapt two explicit GPS CSV layouts

This offline, source-only recipe reuses the position converter, JSONL reader and
adapter SPI. It supplies two profiles, not an automatic firmware detector or a
ULog decoder. Check the actual exporter schema before choosing one. No flight
recording or third-party decoder is included; the example below is synthetic.

| Profile | Boot counter | Latitude / longitude | MSL altitude |
|---|---|---|---|
| `examples/px4-position-csv/legacy.json` | `timestamp`, us | `lat` / `lon`, degE7 | `alt`, mm |
| `examples/px4-position-csv/modern.json` | `timestamp`, us | `latitude_deg` / `longitude_deg`, degrees | `altitude_msl_m`, m |

These explicit layouts follow the PX4 GPS message definitions at
[the integer-coordinate revision](https://github.com/PX4/PX4-Autopilot/blob/1c8ab2a0d7db2d14a6f320ebd8766b5ffaea28fa/msg/sensor_gps.msg) and
[the degree-coordinate revision](https://github.com/PX4/PX4-Autopilot/blob/4842c542b80e7dcc433424984aed98907e054052/msg/SensorGps.msg).
Both definitions list `sensor_gps` and `vehicle_gps_position`; this does not make
their records interchangeable. Preserve the topic, instance, session, firmware and
exporter version with your input. Do not merge or deduplicate them by timestamp.
The shared `position.json` maps converted values to the public common model.

## Run with this repository alone

From the repository root, with the pinned Rust toolchain and normal Cargo cache:

```bash
set -euo pipefail
MUSUBI_EXERCISE_DIR=$(mktemp -d)
python3 - "$MUSUBI_EXERCISE_DIR" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
(p / 'legacy.csv').write_text('timestamp,lat,lon,alt,heading,note\n7,125000000,-452500000,1234,nan,authored\n')
(p / 'modern.csv').write_text('timestamp,latitude_deg,longitude_deg,altitude_msl_m,heading,note\n7,12.5,-45.25,1.234,nan,authored\n')
(p / 'confirmation.csv').write_text('timestamp,latitude_deg,longitude_deg,altitude_msl_m,new_field\n9,-23.5,67.25,-1.5,authored-heldout\n9,-23.6,67.3,-1.6,\n')
PY
cargo build --offline --locked -p musubi-adapter-spi --example ingest_recorded_jsonl
for layout in legacy modern; do
  python3 scripts/convert_position_csv.py "$MUSUBI_EXERCISE_DIR/$layout.csv" \
    "examples/px4-position-csv/$layout.json" "$MUSUBI_EXERCISE_DIR/$layout.jsonl"
  cargo run --quiet --offline --locked -p musubi-adapter-spi --example ingest_recorded_jsonl -- \
    "$MUSUBI_EXERCISE_DIR/$layout.jsonl" examples/px4-position-csv/position.json \
    "synthetic-$layout" 1000 > "$MUSUBI_EXERCISE_DIR/$layout.report.json"
done
python3 scripts/convert_position_csv.py "$MUSUBI_EXERCISE_DIR/confirmation.csv" \
  examples/px4-position-csv/modern.json "$MUSUBI_EXERCISE_DIR/confirmation.jsonl"
cargo run --quiet --offline --locked -p musubi-adapter-spi --example ingest_recorded_jsonl -- \
  "$MUSUBI_EXERCISE_DIR/confirmation.jsonl" examples/px4-position-csv/position.json \
  synthetic-confirmation 1000 > "$MUSUBI_EXERCISE_DIR/confirmation.report.json"
MUSUBI_PUBLIC_REPLAY_ROOT="$PWD" python3 -m unittest discover -s tests -p test_px4_position_profiles.py
```

Expected mapped counts are 1, 1 and 2, with no rejected, untimed or unmapped rows.
The two first positions are identical after declared unit conversion. Confirmation
reuses the modern profile unchanged and retains both equal-time records. `1000`
is an explicit synthetic receipt value, not a measured or inferred observation time.
The regression tests assert source-field preservation, unit/counter meaning, wrong
layout and malformed input rejection and the common-model result.

## Apply to a new recording without overclaiming

1. Obtain an authorized recorded export; retain the original and its rights/provenance.
   A separately qualified exporter is needed for raw ULog. Keep corruption and
   dropout diagnostics and all emitted topic/instance files; do not use an ignore
   option to turn a decoder failure into success. This converter only sees its CSV.
2. Inspect fields against the pinned source definition. Establish boot-clock units,
   angular units and MSL height explicitly. A filename/version label is insufficient.
   Preserve ellipsoid altitude separately; do not substitute it for MSL height.
3. Select one profile, or change just the necessary declared fields/units. Prove the
   old settings reject an incompatible layout, then compare every selected row to
   the common output. Raw CSV sent directly to the JSONL SPI must fail.
4. Freeze the profile and test a separate recording not used to make the change.
   Record the exact confirmed scope and failed/unexamined portions. Shared layouts
   are candidate reuse, not proof for every airframe, firmware, GPS or instance.

No fix/satellite filtering is applied: invalid or uncertain receiver fixes are not
promoted to trustworthy positions. Unselected source fields (including textual
`nan`) remain strings; nonfinite selected coordinates are rejected. Domain remains
Unknown, observed UTC is absent and confidence is zero. Topic/instance provenance,
source fields, counters and profile are unsealed sidecars, not authenticated identity.
Duplicate topics are not independent sensor corroboration. Export-row completeness
does not prove the original flight had no recording gaps. See the shared
[position converter bounds and failure behavior](adapt-position-csv.md).
