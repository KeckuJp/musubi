# Read additional saved declarations and reports

These adapters read files already supplied by a caller. They do not fetch referenced URLs,
subscribe to a device, transmit a command, or verify that a reported state is physically true.
Preserve the original input and the generated report beside each CSV.

## Try an authored VDA factsheet

With Python 3.12, from the repository root:

```sh
python scripts/convert_vda_factsheet_limits.py \
  examples/saved-inputs/authored-vda21-factsheet.jsonl factsheet-output \
  --source-format vda-factsheet-2.1 --capabilities
```

Use a new output directory. This authored fixture is not a device recording. The output
retains the original source and writes geometry, limits, capabilities and an accounting report.
The 2.1 selector preserves the original 2.1 paths and distinguishes `NOT_IN_VERSION` from
missing, empty and false values. The default selector remains 3.0; a version mismatch is refused.
Reported limits are declarations, not measured performance. A 3D URL is retained as text;
the file is not downloaded or decoded. Ambiguous load-speed units remain unresolved unless
the caller explicitly chooses a documented policy.

## Other offline entrypoints

| Saved input | Entrypoint | Boundary |
|---|---|---|
| CAN dictionary and J1939 transfers | `convert_dbc_recorded.py`, `convert_dronecan_capture.py` | Explicit dictionary/source selection; recorded grants and packet accounting do not authenticate a controller. |
| ISOXML, ISOBUS and mission declarations | `convert_isoxml_rate.py`, `convert_isobus_ut_tc_capture.py`, `convert_saved_mission_plan.py` | Keep declarations and reported values separate. No application rate or mission is executed. |
| Nav2, ROS and RMF | `convert_nav2_saved_result.py`, `convert_rosbag_observations.py`, `convert_rmf_path_association.py` | Fixed schema/source selections and explicit associations. Reported action status is not proof of arrival. Panther E-stop is a saved report, not a safety test. |
| FarmBot saved states, logs, pins, points and images | `convert_farmbot_saved_logs.py`, `convert_farmbot_saved_pins.py`, `convert_farmbot_point_list.py`, `convert_farmbot_image_binding.py` | A write response is not a readback. Reported image resources and pin provenance do not establish physical state. |
| AIS and NMEA | `convert_nmea_wind.py`, `convert_nmea_ascii_window.py` | Source fields and finite version meanings only; no vessel identity authentication or navigation advice. |
| Saved video metadata and detector intervals | `convert_video_frames.py`, `check_saved_video_frames.py`, `convert_video_detector_intervals.py`, `convert_video_flight_binding.py` | Caller-supplied frame metadata and declarations. Full PTS/timebase identity is required; no inferred synchronization, interpolation or media decoding. |
| TLOG cross-track report | Rust example `export_tlog_nav_csv` | Preserve reported sign and declared comparison reference. It neither authenticates a route nor recommends a threshold. |

Run each script with `--help` where a command-line entrypoint exists; library-only functions
are exercised in their corresponding tests. Optional dependencies remain in
`scripts/requirements-recorded-tests.txt`. Source pins and finite selectors live in the
adapter and tests, and do not imply support for every version or every writer.

The rational-clock video option keeps exact fractional timestamps. A rounded microsecond
display value alone is insufficient to bind a frame to a flight row. Detector tags describe
what a saved detector reported; they are not verified events or object identities.

Tests use authored fixtures and mocks. Real-device compatibility, external decoder acceptance,
human usability and binary release are not established by these tests. No new viewer behavior
is implied merely by supplying a converter or profile.

`read_telemetry_csv --allow-empty-rows` explicitly accepts a validated header with no rows,
for a declared empty selection. The default still refuses it. Empty bytes, malformed headers
and unsupported options remain errors; an accepted empty selection does not fabricate an observation.
