# Offline adapters, file outputs and verification

These source-preview components process supplied bytes or authored simulations. They do not
connect to a vehicle or send commands. Their tests establish the stated finite input contracts;
they do not establish hardware compatibility, external consumer acceptance or field accuracy.

| Component | Input and result |
|---|---|
| `musubi-adapter-template` | A small text-wire example to copy and adapt; returns sealed evidence or an explicit invalid result. |
| `musubi-adapter-mavlink` | A bounded, passive MAVLink2 byte parser, with raw retention and separate malformed/unmapped outcomes. This is not a complete MAVLink dialect implementation. |
| `musubi-adapter-usv-json` | A declared JSON wire example with missing-field and invalid-input regression cases. It is not a generic USV protocol. |
| `musubi-northbound` | CoT/NVG/NDJSON serialization and in-memory fanout accounting. Methods return bytes; they do not open sockets. |
| `musubi-reference-fanout` | Saved reference observations to four file sinks: CoT, NVG, CSV and a legacy position CSV. |
| `musubi-integration-case` | Local manifest, profile and stored-report validation. Adapter candidates remain HOLD. |
| `musubi-cable-sim` | Seeded, watermarked cable-interaction simulations with explicit unknowns. |
| `musubi-cable-ml-eval` | Repeatable comparison of small methods on those synthetic cases with held-out seeds and abstention. |

## Write reference observations to files

Run from the repository root, using fresh output directories:

```sh
cargo run -p musubi-reference-writers --bin musubi-reference-synth -- --out offline-input --seed 42 --duration-ms 10000 --no-dvr-dir
cargo run -p musubi-reference-fanout -- --in offline-input --out offline-output --profiles profiles
```

Inspect `fanout_ledger.csv` alongside the generated CoT, NVG and CSV files. An output marked
WITHHELD or failed remains an accounted-for outcome. Some sinks cannot carry all trust fields;
the report records omissions or an explicit downgrade. The four-sink reference command has no
network sender. The generic NDJSON serializer in `musubi-northbound` is a separate library API.

Recorded positions use the reader's explicit identity and timestamp semantics. Unknown family,
domain or clock basis is not repaired by a file extension or by a presentation timestamp.
The optional `--near-now --now-ms <epoch-milliseconds>` shifts replay presentation timestamps;
it does not authenticate or improve the source clock. Default operation keeps the saved time.
CoT time, CSV observation time and source uncertainty are distinct values.

## Validate an authored profile example

```sh
cargo run -p musubi-integration-case -- manifest validate --case examples/offline-verification/profile/manifest.json
cargo run -p musubi-integration-case -- profile verify --case examples/offline-verification/profile/manifest.json --candidate-root examples/offline-verification/profile/candidate --against examples/offline-verification/profile/input/sample.csv --gold examples/offline-verification/profile/oracle/profile-oracle.json --requirements-commit 1111111111111111111111111111111111111111 --implementation-commit 2222222222222222222222222222222222222222
```

The all-ones and all-twos values are authored fixture sentinels, **not Git revisions** or claims
of source adoption. The example pins bytes and checks a local report. A report's integrity,
profile score, human acceptance and publication authorization are separate facts. No report
or human-seal field grants execution or publication authority. `report verify` recomputes the
applicable checks from the supplied inputs; it does not simply trust a stored PASS string.
`profile verify` emits a report even when a check fails, so process exit status alone does
not establish a passing result. Read the report's individual checks and disposition.

## Generate and evaluate synthetic cable cases

```sh
cargo run -p musubi-cable-sim -- --scenario normal_drag --tier t1 --seed 42 --out cable-simulation.ndjson
cargo run -p musubi-cable-ml-eval -- --split-seed 17 --train-count 4 --test-count 2 --tier t2
```

The simulator refuses to overwrite an existing output file. Its records are marked SIMULATED.
The profile and field registry under `profiles/public/cable` and `profiles/public/extensions`
are authored declarations used by these examples. Generated labels are simulation assumptions,
not measurements or diagnoses. Evaluation on synthetic seed splits does not demonstrate
generalization to real cables, vehicles or sensors and does not select a deployed model.
The evaluator refuses configurations whose compared methods fail its synthetic acceptance
checks. A refusal is not an evaluation report and must not be recorded as a successful run.

## Adapter extension boundary

Start with `crates/adapter-template/src/lib.rs` and its regression tests. Replace the wire parser
and declare the supported fields and units explicitly. Keep normalization, MARK derivation,
raw-input retention, digest checks and per-sink loss accounting visible. The receive queues
are in-process examples, not live transports. Digest verification proves byte consistency;
it does not authenticate a device or supply signing keys.
