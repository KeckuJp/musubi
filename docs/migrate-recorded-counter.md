# Migrate an explicitly declared recorded boot counter

This offline recipe reuses the JSONL reader and adapter SPI. A field name is not
a clock definition: a field named `TimeMS` may be a week-relative satellite time,
not elapsed time since boot. Successful JSON parsing cannot detect that mistake.
Establish the meaning from the producer's versioned specification before mapping.
No automatic firmware identification or clock inference is provided.

## Investigate and choose the small change

Keep the original input. Record the producer/exporter versions, selected message,
counter field, unit and clock basis, position units/frame, and unknown fields.
Compare old and new schemas, not only version labels. Two different versions may
use exactly the same schema and need the same profile. If no trustworthy boot
field exists, this recipe cannot invent one; use a different honest representation.

`scripts/remap_jsonl_counter.py` changes only the selected message's counter before
the existing reader. Its closed profile has exactly `message_type`, `counter_field`
and `counter_unit` (`ms` or `us`). It does not modify the reader, adapter or core.
Position mapping remains a separate existing adapter profile.

## Reproduce with authored synthetic inputs

From the public repository root, create a new `migration-run/` directory. Never
reuse an evidence directory or overwrite earlier results. Save the following files.
All values here are invented; these are not recordings or firmware support claims.

`migration-run/old.jsonl`:

```jsonl
{"meta":{"type":"META"},"data":{"label":"synthetic"}}
{"meta":{"type":"LOCATION"},"data":{"TimeMS":400000000,"BootMS":1200,"Lat":12.25,"Lng":34.5,"Alt":8,"extra":{"quality":"unknown"}}}
{"meta":{"type":"LOCATION"},"data":{"TimeMS":400000100,"BootMS":1200,"Lat":12.5,"Lng":34.75,"Alt":9}}
```

`migration-run/new.jsonl`:

```jsonl
{"meta":{"type":"LOCATION"},"data":{"TimeUS":2500000,"Lat":-10.5,"Lng":20.25,"Alt":5,"extension":[1,2]}}
{"meta":{"type":"LOCATION"},"data":{"TimeUS":2499000,"Lat":-10.25,"Lng":20.5,"Alt":6}}
```

`migration-run/heldout.jsonl` (reserve until the first two pass):

```jsonl
{"meta":{"type":"LOCATION"},"data":{"ElapsedMS":9876,"Lat":1.5,"Lng":2.25,"Alt":7,"future":true}}
```

Save the three counter profiles as `old-counter.json`, `new-counter.json`, and
`heldout-counter.json` in that directory, respectively:

```json
{"message_type":"LOCATION","counter_field":"BootMS","counter_unit":"ms"}
```

```json
{"message_type":"LOCATION","counter_field":"TimeUS","counter_unit":"us"}
```

```json
{"message_type":"LOCATION","counter_field":"ElapsedMS","counter_unit":"ms"}
```

Save `migration-run/position.json`:

```json
{"message_type":"LOCATION","latitude_field":"Lat","longitude_field":"Lng","altitude_field":"Alt","position_units":"deg-m-msl"}
```

The synthetic positions are declared WGS-84 degrees and metres above mean sea level.
Real input requires independent evidence for that declaration. Local-map metres
must not be labeled latitude/longitude. Run, stopping if any command fails:

```sh
set -e
for name in old new heldout; do
  python3 scripts/remap_jsonl_counter.py migration-run/$name.jsonl migration-run/$name-counter.json migration-run/$name-mapped.jsonl
  cargo run --quiet --locked -p musubi-adapter-spi --example ingest_recorded_jsonl -- migration-run/$name-mapped.jsonl migration-run/position.json synthetic-$name 1789084800000 > migration-run/$name-report.json
done
MUSUBI_PUBLIC_REPLAY_ROOT="$PWD" python3 -m unittest discover -s tests -p test_jsonl_counter_remap.py
```

Use the fixed ingestion timestamp only for this deterministic synthetic replay;
it is not a measured historical receipt time. Inspect all reports: old has three
retained records, one untimed and two mapped with counters 1200000 and 1200000;
new has two mapped counters 2500000 and 2499000 in source order; heldout has one
mapped counter 9876000. All positions match their source values. No row is sorted
or deduplicated. Domain stays Unknown, observed UTC remains absent and confidence
does not increase. `source_record` contains the entire original selected record,
including the misleading old `TimeMS`. All other message records are unchanged
as parsed values. Run old again to a **new** output file and compare its bytes.

As negative controls, applying the old counter profile to the new input must fail
with no output file; remapping a previously mapped input must fail on the reserved
`source_record` collision. Malformed/truncated JSON, duplicate keys, nonfinite
values, missing counters, bool/fractional/negative counters and overflow fail.

## Limits and migration discipline

- The complete input is validated before opening a new output file. Existing
  outputs are refused. An operating-system write failure can leave a partial new
  file: check exit status, retain failed evidence separately and do not use it.
- Bounds: input/output 16 MiB, line 64 KiB, profile 4096 bytes, nesting depth 32.
  Preserving the original can make a previously fitting line exceed output limits;
  it fails rather than silently dropping source fields.
- Selected counters are unsigned 64-bit microseconds after scaling. Other values
  are retained as parsed JSON, not original bytes or arbitrary precision across
  the Rust reader. The reader's 64-bit integer and finite-f64 limits still apply.
- The preserved source, profiles, counters and report indices are unsealed
  sidecars. Common-model envelope digests do not authenticate the original input.
- A declared but semantically wrong counter profile may still parse successfully.
  Check the counter against an independent specification/oracle, not the same
  conversion formula alone. Do not treat satellite time as boot or UTC time.
- This is not raw binary decoding, live device integration, field acceptance,
  automatic version support, or proof of clock accuracy. No decoder is bundled.

For a real migration, pin the two originals and exporter, compare units/counters
independently, reserve another input before implementation, and repeat the old
regression after changing the profile. Keep recordings and internal evidence out
of public fixtures. Report the changed layer, any core change, human intervention,
elapsed time, tested scope and unresolved meanings separately from support counts.

## A new Codex or Claude Code session

Use a public-only working copy and authorized synthetic inputs. Both tools follow
the same task; neither is a runtime dependency or publication authority.

> Read this recipe and docs/adapt-recorded-input.md without earlier chat history.
> Execute the old and new synthetic migrations, then the held-out migration using
> only a profile change. Verify every expected counter, position, retained field,
> accounting category, domain and clock confidence. Run the old-input regression,
> wrong-profile and source-record-collision controls and the indicated tests.
> Save exact commands and actual results in the new migration-run directory.
> Do not change source, core, dependencies, publish, access recordings, contact a
> device or read outside this public workspace. Finish after this bounded replay.

The saved commands should work in another session without private source or this
conversation. Synthetic provider replay and separately checked real-input results
are distinct evidence; neither establishes independent human maintainability.
