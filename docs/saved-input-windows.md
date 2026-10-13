# Read explicit parts of saved inputs

These offline entrypoints select a finite part of a saved file, or keep explicitly named
senders separate. They do not acquire a live stream, authenticate a device, synchronize
clocks, or send commands. Keep the original file: a digest identifies bytes, not their truth.

## Start with an authored CAN sample

From the repository root, with Python 3.12:

```sh
python scripts/convert_candump_recorded.py \
  examples/saved-inputs/authored-can-window.candump can-window-output \
  --interface can0 --start-line 0 --stop-line 3
```

Use a fresh output directory. `capture.csv` contains two selected data frames. `report.json`
accounts for three selected physical lines, one excluded line, and the other interface.
The original line indices are one-based; the requested window is zero-based and excludes
the stop index. A decreasing timestamp on the unselected interface is retained and counted;
a decreasing selected-interface timestamp is refused. This sample was authored for tests.

## Choose an entrypoint

| Saved input | Entrypoint | Selection and limits |
|---|---|---|
| ROS1/ROS2 bag, MCAP, file-zstd bag | `convert_rosbag_observations.py`, `convert_rosbag_zstd_window.py` | Explicit capture-time interval and optional topics. The compressed and expanded sizes are bounded separately. Header time remains separate from capture time. |
| Native IMC or gzip LSF | `convert_imc_sensors.py`, `convert_imc_gzip_window.py` | Native record indices; optional explicit source system. Gzip has compressed, expanded and member-count limits. |
| Saved PCAP/PCAPNG UDP and finite TCP captures | `convert_udp_payload_window.py`, `convert_pcapng_udp.py`, `convert_imc_tcp_recorded.py` | Explicit saved packet/record selection. No socket is opened. TCP reassembly remains finite and refuses unsupported ambiguity. |
| farm-ng EventLog | `export_farmng_eventlog_window.py` | Explicit saved record selection, source digest and original offsets. Reported recorder identity is not authenticated hardware identity. |
| CAN, MOOS, Signal K, NMEA, VDA, steering and decoded Blackbox | The corresponding `*_window.py` or existing converter's window options | Original line/record accounting stays in the report. Selection does not establish the meaning of excluded records. |
| Saved Rover/Copter DataFlash | `convert_rover_saved_bundle.py` | A fixed source declaration is required. GPS receiver instances stay separate; truncated-tail tolerance requires an explicit record window. |
| Saved Sub JSON/JSONL/BIN | `convert_ardusub_session.py`, `convert_ardusub_session_window.py`, `convert_ardusub_bin_window.py` | Fixed source declaration and an explicit single-boot session. Surface anchors must be declared and bound to the selected original; no depth datum is guessed. |

Run each script with `--help` for its exact parameters. Install the optional packages in
`scripts/requirements-recorded-tests.txt` for the examples that require them. Limits and
refusals remain active even when the requested interval is small. Some paths scan or expand
the entire bounded source to account for what was excluded; “window” does not mean random
access or unlimited input size. An empty selection may be refused by a particular converter.

Plane, Rover, Copter and Sub meanings are selected from finite writer/version declarations.
Names in a saved `VER`, `MSG`, `PARM` or profile do not prove the physical platform. Existing
raw/default behavior remains available. Domain qualification is an explicit option; no
vehicle commands are carried by these converters.

## Separate saved TLOG senders

`musubi_reference_readers::tlog_sender_batch::read_with_limits` accepts caller-declared
sender profiles. Each must name a distinct, nonzero system/component pair. It preserves
unknown message bytes and reports selected and unselected counts. All-or-error behavior
prevents a failed later sender from appearing as a successful partial batch.

`musubi_reference_pipeline::tlog_asset_batch::analyze_recorded` keeps the recorded clock
Unknown and analyzes each explicitly labelled asset separately. `analyze_declared` is a
different entrypoint: it requires a caller-supplied host-receipt basis before using that
clock interpretation. Labels and header tuples are not authenticated identity. These APIs
read in memory with input, aggregate-read and observation budgets; they are not a live or
constant-memory transport. The two analysis passes count toward the aggregate budget.

## Export declared domains explicitly

`export_all_with_declared_domain` in `musubi_reference_pipeline::export` adds domain, basis
and provenance columns under CSV schema version 4. Conflicting declarations remain unresolved.
The existing `export_all`, `absences_csv` and `claims_csv` keep their prior default output.
The reference viewer does not automatically select the new export function.

The common `PlatformDomain` enum now includes `Subsurface`. Existing canonical domain tags
retain their values; the new tag is additive. Rust consumers with exhaustive matches must
handle the new variant. No northbound symbol, transport or physical identity is implied.
The generic `recorded-readers` JSON profile API does not automatically inherit all of the
reference pipeline's TOML domain options.

`musubi_decoded_csv::ParseOptions` has an explicit `allow_empty_rows` field, false by default.
Consumers constructing the struct must include it or use `..Default::default()`. Enabling
it accepts a validated header without rows, not empty bytes or malformed input. Existing
command-line common-observation ingestion keeps this option disabled.

Tests use authored fixtures and mocks. They do not establish device compatibility,
external decoder acceptance, UI usability, or a binary release.
