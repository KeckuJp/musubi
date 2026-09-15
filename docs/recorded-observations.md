# Read saved inputs as common observations

The recorded readers and adapter SPI connect saved records to one meaning-labelled
observation report. Use this route for electrical quantities, local camera poses,
reported state and declared values that are not geodetic platform positions.
The existing position adapter remains available for its explicitly declared inputs.

This report is a common offline observation representation. It is **not** a sealed
`EvidenceEnvelope`, a CoT/NVG message, or a diagnosis. No input causes a command or
network connection. A value labelled as reported remains a publisher's report.

## Battery values, from CSV to meaning

Run from the repository root. Create `battery.csv` containing this authored example:

```csv
TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct
1000,0,12.5,2,100,1,80
2000,0,11.75,-1.2,101,1.1,79
```

```sh
python3 -m scripts.convert_ardupilot_battery_csv --help
python3 -m scripts.convert_ardupilot_battery_csv battery.csv normalized.csv
cargo run -p musubi-adapter-spi --example ingest_recorded_observations -- \
  normalized.csv profiles/recorded/ardupilot-battery/profile.json \
  profiles/meanings/reported-quantities.json recorded-battery 1700000000123 \
  > observations.json
```

The two rows retain 12.5 V / 2 A / 25 W and 11.75 V / -1.2 A / -14.1 W.
Power is labelled `calculated`; voltage and current are `reported`. The converter
retains the original columns, converts declared quantities, and preserves explicit
missing-value representations. The supplied integer is the ingest receipt time in
Unix milliseconds; it is not a replacement for the source clock.

Change the input values and run the same converter, reader and meaning declarations
again in a new output location. Select a converter's exact `--source-format` from
its help when the input uses another qualified layout; a filename or device family
is not enough to select a schema. Do not feed unconverted native battery encodings
into the normalized meaning profile.

## Saved camera trajectory

A TUM-style trajectory has timestamp, tx/ty/tz and quaternion x/y/z/w values. For
an already-qualified metric RGB-D camera trajectory with the declared first-keyframe
world convention:

```sh
python3 -m scripts.convert_pose_text pose.txt pose.csv --report pose-report.json
cargo run -p musubi-adapter-spi --example ingest_recorded_observations -- \
  pose.csv profiles/recorded/pose-text/orbslam2-profile.json \
  profiles/meanings/saved-camera-pose.json recorded-camera 1700000000123 \
  > camera-observations.json
```

The subject remains a camera in its declared local frame. The profile does not
convert that pose into a vehicle, WGS-84 or ENU position. Quaternion convention and
metric scale are input requirements, not inferred properties. The source time stays
Unknown, with zero time confidence and no boot or wall anchor. A timestamp-shaped
number is still retained in its original fields. Lost tracking frames absent from
an exported trajectory cannot be reconstructed by this reader.

## Reuse the same connection

Reader JSON declares `profile_id`, `version`, `family`, `source_role`, `format`,
`extensions`, `default_clock_basis`, `channels`, `fields` and `units`. These are
explicit interpretation inputs. Duplicate JSON object keys are rejected at every nesting
level in both reader and meaning declarations. There is no directory override or device discovery.
Available formats are `telemetry_csv_us`, `blackbox_decoded_csv`,
`pymavlink_dataflash_jsonl`, `mavlink_tlog`, `ardupilot_dataflash_bin`, `px4_ulog`,
`edgetx_csv`, and `miniseed2`. The miniature waveform decoder retains instrument
counts; it does not apply an instrument response or claim SI measurements.

Meaning JSON has `schema: recorded-meanings/v1`, a `subject` with `kind`, `frame`,
`convention`, and a `fields` object. Each entry names an **exact source field** and
supplies `meaning`, `unit`, and `basis` (`reported`, `declared`, or `calculated`).
Author these declarations against the selected converter's output contract. Units
and meanings are never inferred from suffixes. A CSV header unit contradicting a
meaning declaration is rejected. Optional fields absent from one recording do not
create observations. Fields absent from the meaning map are retained with null
meaning; their source profile annotations remain visible as `source_unit`.

Every observation includes its source, channel, clock basis, raw time, subject and
ordered fields. Blank and retained nonfinite text are distinct from zero. Use
`--allow-equal-time` for source-order-only equal timestamps and
`--preserve-nonfinite-as-text` for the explicit CSV nonfinite policy. Decreasing CSV
time remains invalid. These options apply only to `telemetry_csv_us`.

Input bytes are retained once as hexadecimal alongside SHA-256 of the exact input,
reader profile and meaning profile. A hash identifies bytes; it does not establish
authenticity, approval, or the truth of declared meanings. `observation_index` orders
decoded observations, not physical packets. Native records may expand into several
observations; do not treat the observation count as a frame, row or sample count.
The report carries separate counts where a decoder exposes them. DataFlash untimed
records retain their decoded fields in `accounting.untimed_records`; ULog unsupported
records retain a reason in `accounting.unsupported_records`. Each entry has `offset`
and `byte_length` into the retained input. Neither becomes a synthetic timed
observation. ULog selects instance 0 and separately counts decoded and total data
records. Unmapped raw records remain recoverable from the retained source. Unknown MAVLink message CRC
extras and MAVLink signatures are not verified by this reader.

## Other converters and optional dependencies

Run Python converters as modules from the repository root: `python3 -m scripts.NAME
--help` (the existing position and position-batch converters print positional usage on missing arguments). The saved-input helpers cover electrical/state exports, recorded ROS bags,
passive CAN exports, recorded marine messages, declared GPX geometry, ISOXML rates,
OPC UA/DataValue exports, MQTT envelopes, and saved video/detection metadata. Their
options specify qualified input layouts and bounds. These are saved-file conversion
paths, not live drivers or device compatibility certification.

Some formats need separately installed Python packages. See
[Recorded-input dependencies](recorded-dependencies.md). No vendor SDK executable,
recording or generated third-party schema is bundled. Keep actual recordings and
reports in a location appropriate for their contents; retained fields can identify
the source device or operator.

The connection accepts at most 16 MiB of input and 100,000 decoded observations.
The CLI refuses a report over 128 MiB. It reads whole selected inputs; it is not a
streaming reader for arbitrarily large bags. Use the converter's documented interval
or topic selection before ingestion, or explicitly segment inputs with their
boundaries retained. A failure produces no successful partial report; the CLI's
error is fixed text in JSON, without copying the input or file paths.

## Reuse decoded JSONL for battery and state conversion

For a saved `pymavlink_dataflash_jsonl` export, first use the common CLI with
`profiles/recorded/arduplane-jsonl/profile.json` and explicit meaning declarations
(an empty `fields` map is allowed for raw inspection). Its report retains parsed
`source_records` and `value_basis: pymavlink_decoded_no_second_wire_scaling` alongside
the original bytes. These are decoded values; do not apply wire scaling a second time.

```sh
python3 -m scripts.normalize_ardupilot_report decoded-report.json battery-output \
  --battery-format ardupilot-bat-4.3
cargo run -p musubi-adapter-spi --example ingest_recorded_observations -- \
  battery-output/observations.csv profiles/recorded/ardupilot-battery/profile.json \
  profiles/meanings/reported-quantities.json recorded-battery 1700000000123
```

The new output directory retains the input report, selected CSV and selection
accounting. The helper also supports explicitly selected MODE, ARM, PARM and PM
layouts; inspect its help. For an exporter containing literal NaN/Infinity tokens,
`python3 -m scripts.normalize_pymavlink_jsonl INPUT OUTPUT` preserves those literals
as text and retains their original lines. Numeric overflow and duplicate JSON keys
are rejected. This does not make a nonfinite measurement usable; parameter default
handling requires the helper's explicit preservation option.
