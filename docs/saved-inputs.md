# Saved input declarations

These tools read files already saved on disk. They do not connect to a vehicle, request data,
execute a saved plan, or certify that a reported condition actually occurred.

Use `python scripts/<converter>.py --help` for the exact input schema, explicit clock and format
options, and refusal conditions. Converter tests construct synthetic inputs; those tests are
not device compatibility or field validation.

A profile is a declaration. Select it only when its format and source semantics match your
input. `profiles/declared/` contains complete TOML declarations for the reference analysis CLI;
`profiles/recorded/` contains the supported JSON reader declarations for the recorded report
interface. A TOML-only format is not accepted by the JSON reader merely because a profile exists.
`profiles/meanings/<case>/<profile>.json` preserves the corresponding declared unit vocabulary.
The subject frame remains unspecified; a filename or product family does not establish identity,
coordinate frame, physical state, or time synchronization.

For a converter producing telemetry CSV, the report path is:

```sh
cargo build --workspace --all-targets --locked
python scripts/<converter>.py <arguments from --help>
target/debug/examples/ingest_recorded_observations converted.csv \
  profiles/recorded/<case>/<profile>.json profiles/meanings/<case>/<profile>.json \
  saved-source 1700000000000 --allow-equal-time > observations.json
target/debug/musubi-reference-viewer --report --in observations.json --check
target/debug/musubi-reference-viewer --report --in observations.json
```

Use `--allow-equal-time` only for a source whose repeated counters must be retained. Do not use a
converter's diagnostic JSON as the viewer report. JSONL output requires its matching JSONL reader
profile, not a CSV declaration. Retained non-finite source tokens require the explicit
`--preserve-nonfinite-as-text` option where supported; they are not finite measurements.

For the complete TOML analysis declarations, use the existing reference CLI:

```sh
target/debug/examples/read_telemetry_csv profiles/declared/<case>/<profile>.toml converted.csv
```

Its diagnostic JSON and the `recorded-observation-report/v1` viewer input are distinct interfaces.
Neither output is a sealed common evidence envelope. Check counts and refusal results before
using an output. Unknown clocks remain unknown; declared relative-clock anchors do not prove
wall-clock synchronization. Sender selection in a TLOG profile chooses a declared system and
component pair; it is not sender authentication.

The recorded report interface bounds input to 16 MiB, declarations to 256 KiB and output to
128 MiB. A converter accepting a larger file does not remove those downstream limits. Keep the
original saved input and its provenance; generated examples contain no retained field recordings.

## Two synthetic examples

From the repository root, with Python 3.12 and the built workspace:

```sh
python scripts/convert_gcs_tlog_csv.py examples/saved-inputs/authored-gcs.csv gcs.csv \
  --source-format missionplanner-1.3.82-tlog-csv --selected-message mavlink_attitude_t \
  --decimal-separator dot
target/debug/examples/ingest_recorded_observations gcs.csv \
  profiles/recorded/gcs-csv-reuse/attitude-profile.json \
  profiles/meanings/gcs-csv-reuse/attitude-profile.json authored-gcs 1700000000000 > gcs-report.json
target/debug/musubi-reference-viewer --report --in gcs-report.json --check

python scripts/convert_rtklib_solution.py examples/saved-inputs/authored-rtklib.pos rtklib-output
target/debug/examples/ingest_recorded_observations rtklib-output/observations.csv \
  profiles/recorded/rtklib-solution/profile.json profiles/meanings/rtklib-solution/profile.json \
  authored-rtklib 1700000000000 > rtklib-report.json
target/debug/musubi-reference-viewer --report --in rtklib-report.json --check
```

The RTKLIB path reads an existing LLH solution. It performs no RTK/PPK solving, geoid correction,
datum conversion or time-system conversion. Solution quality is the producer's report, not a
verified accuracy claim. Both examples above are authored, not surveyed positions or recorded flights.
Other selected formats have synthetic construction and refusal examples in `tests/test_*.py`.

The receipt timestamp in these synthetic commands is an explicit example, not a reconstructed source time. For real use supply the actual receipt timestamp. Verbose source declarations beyond the meaning-alias limit remain intact in `source_unit`; they are not shortened or promoted into a new meaning.

## Saved point clouds in bounded reports

The LAS converter reads supported saved LAS 1.4 point-record formats 0 and 1. It does not sample,
thin, solve, reproject or certify points. Coordinate-reference qualifications remain declarations.

```sh
python scripts/export_point_cloud_chunks.py saved.las cloud-chunks \
  --capture-time-us 1700000000000000 --points-per-chunk 1024
python scripts/ingest_point_cloud_chunks.py cloud-chunks \
  profiles/recorded/saved-point-cloud/profile.json profiles/meanings/saved-point-cloud/profile.json \
  cloud-reports --ingest-bin "$PWD/target/debug/examples/ingest_recorded_observations" \
  --viewer-bin "$PWD/target/debug/musubi-reference-viewer"
target/debug/musubi-reference-viewer --report --in cloud-reports/report-000000.json
```

Supply your own capture declaration; the example timestamp is synthetic. The driver records the
actual ingestion receipt time. Neither timestamp reconstructs when the points were surveyed.
The viewer opens one report at a time, not a combined point-cloud map. Every report includes a
file declaration; repeated declarations across chunks are not extra points.

The exporter defaults to 1,024 points per segment for the report interface. A point-count setting
alone cannot guarantee byte bounds for arbitrary declarations. Oversized segments are refused;
repeat with a smaller setting in a **new** output directory. An empty cloud still produces one
file-declaration report. At most 4,096 segments are accepted. There is no automatic sampling.

Only `cloud-reports/manifest.json` marks completion: it is produced after every segment's CSV
hash, byte count, consecutive point range, common-observation count, and viewer validation pass.
If a segment is missing, modified or rejected, partial reports may remain but there is no new
completion manifest. Check the exit status and completion manifest; directory existence is not
success. The manifest is an integrity/accounting record, not an authenticated source certificate.

Processing holds one segment/report at a time. Keep enough disk for all reports, including the
embedded source bytes and semantic declarations. Do not concatenate the reports into a larger
viewer input or interpret a tested synthetic capacity as a guarantee for every LAS producer.
