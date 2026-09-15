# Inspect saved observations with the reference viewer

The native viewer opens local files with no account, server, device connection or control interface.
It is a source preview for inspecting recorded data and synthetic analysis. It is not a binary release,
a validated diagnostic product, or a certification of a device or an operator decision.

Use the pinned Rust toolchain from the repository root. Building downloads the locked Cargo dependencies;
the running application has no network API. On Linux a working graphical desktop and OpenGL are needed.
Windows and macOS use their native window systems. See the dependency notices in `NOTICE`.

## First view: a complete synthetic example

```sh
cargo run -p musubi-reference-viewer -- --report --in samples/viewer/observations.json
```

This bundled two-row report is synthetic. Click a row to inspect the source value, source unit,
explicit meaning, meaning unit, basis, missing-value disposition, clock and provenance.
The viewer checks the embedded source bytes against their digest. The profile/meaning hashes are
reported declarations because their original files are not embedded. Hash consistency is not authentication.
Long values are visibly shortened on screen; the complete original JSON stays in the input file.
Rows stay in file order. Unknown clocks are not converted to UTC, and null is not zero.

To validate without opening a window:

```sh
cargo run -p musubi-reference-viewer -- --report --in samples/viewer/observations.json --check
```

To rebuild the example from the bundled CSV, using the existing converter and public ingestion API:

```sh
python3 -m scripts.convert_ardupilot_battery_csv samples/viewer/battery.csv battery-normalized.csv
cargo run -p musubi-adapter-spi --example ingest_recorded_observations -- \
  battery-normalized.csv profiles/recorded/ardupilot-battery/profile.json \
  profiles/meanings/reported-quantities.json synthetic-battery 1700000000123 > battery-observations.json
cargo run -p musubi-reference-viewer -- --report --in battery-observations.json
```

The same `--report` mode opens the outputs in [the common observation guide](recorded-observations.md).
It accepts `recorded-observation-report/v1` with `com_sealed: false`. It rejects malformed or duplicate-key
JSON, count/source inconsistencies and inputs above 128 MiB (embedded sources above 16 MiB or more than
100,000 observations). Empty reports show an explicit empty state. An input failure exits with a reason;
no observation is silently skipped in this mode. Do not modify the file while opening it.

## Existing multi-record analysis

The reference analysis view has a different input contract: a directory of saved logs and TOML profiles.
It estimates clock offsets, marks missing observations and shows consistent/ambiguous/unknown cause candidates.
These are model-dependent hypotheses, not confirmed physical causes. This analysis is not run on report JSON.

Generate a reproducible demo before opening its folder:

```sh
cargo run -p musubi-reference-writers --bin musubi-reference-synth -- --out viewer-demo --seed 42
cargo run -p musubi-reference-viewer -- --in viewer-demo --profiles profiles
cargo run -p musubi-reference-viewer -- --in viewer-demo --profiles profiles --export viewer-export
```

The folder example has three synthetic assets and seven record files. The screen retains the timeline,
clock uncertainty, absence tracks, cause alternatives, provenance and explanatory guide. The export has
`absences.csv`, `cause_claims.csv`, `answer.txt` and synthetic comparison metrics when ground truth exists.
The bundled `profiles/public` are examples, not evidence about your equipment. The optional local
`profiles/partner` override directory is not distributed. Review any custom profile before use.

## Detached observation records

```sh
cargo run -p musubi-reference-viewer -- --detached --in observations.ndjson
```

This separate mode accepts only `ObservationExtensionV1` records from `musubi-field-contracts`.
It preserves event-clock basis, missing values, semantic identifiers and raw-artifact references, without
clock alignment or cause analysis. Other sidecar record variants are rejected by the viewer. The library
also declares annotations, registries and verification records; declaration/validation is not a seal,
human approval, transport implementation or permission to execute anything.

`--screenshot image.png` can be added to a graphical invocation. It opens a window, saves one frame and
exits. It requires a desktop; headless tests do not establish graphical platform compatibility or human UX acceptance.
