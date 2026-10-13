# Saved reports and explicit comparisons

These tools read files supplied by the caller. They do not collect host state, contact a
device, send a request, set a parameter, or infer that a reported condition physically occurred.
Each converter accepts a finite source format/version and writes its accounting and source
digest beside the output. Unsupported, malformed and unbound input is refused or retained in
an explicitly named unsupported category; it is not silently turned into an observation.

## Try an authored storage report

The sample is synthetic. Its `source_format` is an explicit interpretation selector, not
authentication of the producer. Python 3.12 and the pinned Rust toolchain are required for
this example; it adds no optional decoder package.

```sh
cargo build -p musubi-reference-readers --examples
python3 scripts/convert_saved_filesystem_capacity.py \
  examples/saved-inputs/authored-filesystem.jsonl /tmp/musubi-storage-report
target/debug/examples/read_telemetry_csv \
  profiles/declared/saved-filesystem-capacity/profile.toml \
  /tmp/musubi-storage-report/observations.csv --allow-equal-time
```

Use a new output directory. The converter refuses to overwrite one. It retains the original
bytes as `source.jsonl`. In this sample, 400 bytes are free to root, 350 are available to an
unprivileged user, and the 50-byte difference is not a storage failure. The reported 63.2%
uses `used / (used + user_available)`, not `used / total`. Replacing it with 60% is refused.
The capture clock and platform domain stay Unknown. These values do not guarantee allocation
capacity or establish physical disk health.

## Choose the declared meaning

| Saved input or question | Entry points | Limit of the result |
|---|---|---|
| Host, storage and lifecycle reports | `convert_saved_filesystem_capacity.py`, `convert_saved_storage_state.py`, `convert_saved_psu.py`, `convert_journal_coredump.py`, `convert_journal_host_lifecycle.py`, `convert_strace_storage.py` | Reported counters/events, not acquisition or root-cause diagnosis |
| Explicit expectation or identity comparison | `check_saved_topics.py`, `check_identifier_uniqueness.py`, `compare_producer_software_version.py`, `convert_declared_expectation_check.py` | Caller-declared scope and exact source binding; not authenticated identity or general compatibility |
| Saved protocol/report composition | `convert_rtps_data.py`, `convert_mavsdk_grpc_sequence.py`, `convert_onvif_report_set.py`, `convert_request_response_match.py` | Recorded payloads and explicit pairings; no live connection or request delivery |
| Geometry, images and point clouds | `convert_saved_mission_plan.py`, `convert_sdf_airframe.py`, `convert_image_original_binding.py`, `convert_realsense_pointcloud.py` | Declared frames/geometry and supplied originals, not surveyed truth |
| Video cues and clock relationships | `convert_saved_video_filter_log.py`, `convert_video_cue_flight_binding.py`, `convert_rtp_sender_clock.py`, `check_saved_observation_age.py` | Exact supplied timestamps and declared clock basis, not measured synchronization |

Run an entry point with `--help` and use its matching `profiles/declared/` declaration. File
selection and clocks are explicit. A common module may support several versions, but a
successful example does not establish every source, firmware, writer or device.

## Recorded failure candidates and refusals

`read_telemetry_csv --recorded-failure-candidates` connects qualified saved report fields to
a separate, finite catalog. Optional context must bind to the exact record and declared
expectation. A candidate, disagreement, reported error or typed refusal is not a physical
failure verdict. The default temporal catalog and default reader behavior are unchanged.
The added `StorageFailure` and `ConfigurationError` enum variants are recorded-only:
synthetic scenario injection rejects them. Downstream exhaustive matches need updating.

Framed MAVLink JSON input can be read through `read_mavlog_stream`; accepted frames can be
emitted before EOF, so consumers must handle a later refusal without treating partial output
as complete. Unrepresentable clock-alignment arithmetic abstains instead of wrapping or
saturating a result into a plausible timestamp.

The supplied fixtures and regression tests establish bounded source behavior. Real recordings,
external decoder runs, hardware compatibility and human usability need separate evidence.

## Rover 4.5.0–4.5.5 BAT/BCL/BCL2 qualification

After explicitly selecting the matching saved Rover writer schema, use
`profiles/declared/ardupilot-battery/rover-profile.toml` for a caller-declared Ground source.
Use `unknown-profile.toml` when that platform-domain declaration is unavailable. A Plane
profile must not supply the domain for this route. BCL2 cell reports preserve missing and
reported-zero cells separately; they do not fabricate a pack voltage or current.
