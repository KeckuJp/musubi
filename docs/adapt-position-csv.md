# Adapt a recorded position CSV

Use this offline path when a decoded CSV contains documented geodetic position but
the existing JSONL reader needs different field names or numeric units. A small
conversion feeds the existing adapter SPI and public `PlatformState`; neither the
reader nor the core needs to change. All examples below are authored synthetic data.
This is a source-preview development recipe, not live-device or firmware acceptance.

## 1. Investigate and choose the smallest change

Keep the original bytes. Establish the format, counter basis, units, coordinate
frame, altitude reference and data rights from the producer's documentation.
Plausible values and names such as `GPS`, `Alt` or `timestamp` do not establish them.

- Already compatible JSONL: use the [existing mapping recipe](adapt-recorded-input.md).
- Decoded CSV with supported position units: use the nine-key conversion profile below.
- Unsupported binary format: first use an independently checked decoder; CSV conversion
  does not decode a recording.
- Local coordinates, ellipsoidal height or height above home: do not declare them to
  be geodetic coordinates and mean-sea-level height. This converter has no datum or
  transform to make those meanings equivalent.

The converter supports counters in `us` or `ms`, angles in `deg` or `degE7`, and
altitude in `m`, `dm` or `mm`, with `altitude_reference` exactly `msl`. It infers no
source identity, fix quality, absolute time or platform domain. A wrong but syntactically
valid declaration can succeed; numerical validation cannot prove the stated meaning.

## 2. Convert the first input

From the repository root, create a fresh directory with `mkdir position-example`.
If that name exists, choose an unused name consistently below. Save this CSV as
`position-example/first.csv`:

```csv
elapsed_ms,lat_e7,lon_e7,height_mm,export_note
1001,122500000,345000000,8250,"first, authored"
1001,125000000,347500000,9000,synthetic-only
```

For this invented exporter, `elapsed_ms` is a counter in milliseconds, latitude and
longitude are WGS-84 degrees multiplied by ten million, and height is millimetres
above mean sea level. Save `position-example/conversion.json`:

```json
{
  "message_type": "POSITION",
  "counter_field": "elapsed_ms",
  "counter_unit": "ms",
  "latitude_field": "lat_e7",
  "longitude_field": "lon_e7",
  "angle_unit": "degE7",
  "altitude_field": "height_mm",
  "altitude_unit": "mm",
  "altitude_reference": "msl"
}
```

All nine keys are required, with no extra or duplicate keys. Values are nonempty
strings of at most 256 characters without control characters. `message_type` must
be 1–256 ASCII characters matching `[A-Za-z0-9_.:-]`. Column selection matches the
complete, case-sensitive name after trimming surrounding header whitespace; unit
suffixes are not stripped automatically.

```sh
python3 scripts/convert_position_csv.py position-example/first.csv position-example/conversion.json position-example/first.jsonl
```

Exit 0 means a new JSONL file was written after the complete input was validated.
Each row has `meta.type` and `data.TimeUS`, `lat_deg`, `lon_deg`, `alt_m`, plus
`data.source_record`. The latter retains every original column's header spelling
and parsed cell text, including unknown columns. CSV quoting is decoded: the first
`export_note` becomes the string `first, authored`. Keep original bytes for exact
whitespace/quoting preservation. Equal and decreasing counters stay in source order;
the converter does not sort, deduplicate or infer chronology.

## 3. Connect to the public common position model

Save the separate adapter mapping as `position-example/mapping.json`:

```json
{"message_type":"POSITION","latitude_field":"lat_deg","longitude_field":"lon_deg","altitude_field":"alt_m","position_units":"deg-m-msl"}
```

```sh
cargo run --quiet --locked -p musubi-adapter-spi --example ingest_recorded_jsonl -- position-example/first.jsonl position-example/mapping.json synthetic-position-a 1789084800000 > position-example/first-report.json
```

The last argument is ingestion time in Unix **milliseconds**. This fixed value is
only a deterministic synthetic replay input, not a measured historical receipt.
Expect two records and two mapped envelopes, with no rejected, untimed or unmapped
records. Both same-time observations remain distinct. Each envelope carries a
public `PlatformState`, a MARK and content digest. Domain remains `Unknown`,
`observed_at` is null, and time/claim confidence is zero. No fix policy is configured;
retaining a satellite count would not make it a valid-fix assertion.

## 4. Reuse unchanged settings on a second input

Save `position-example/second.csv`. Its new column requires no profile change:

```csv
elapsed_ms,lat_e7,lon_e7,height_mm,export_note,vendor_future
2002,-105000000,202500000,5000,another-authored-input,new
1999,-102500000,205000000,6000,order-retained,unknown
```

```sh
python3 scripts/convert_position_csv.py position-example/second.csv position-example/conversion.json position-example/second.jsonl
cargo run --quiet --locked -p musubi-adapter-spi --example ingest_recorded_jsonl -- position-example/second.jsonl position-example/mapping.json synthetic-position-b 1789084800000 > position-example/second-report.json
```

Expect another two records and two mapped envelopes, no other outcomes, and both
`vendor_future` values retained in `records[*].data.source_record`. Check these
mapped values in the two reports; the second input's counter regression is retained:

| Input / source index | Counter (µs) | Latitude (deg) | Longitude (deg) | MSL altitude (m) |
|---|---:|---:|---:|---:|
| First / 0 | 1,001,000 | 12.25 | 34.5 | 8.25 |
| First / 1 | 1,001,000 | 12.5 | 34.75 | 9 |
| Second / 0 | 2,002,000 | -10.5 | 20.25 | 5 |
| Second / 1 | 1,999,000 | -10.25 | 20.5 | 6 |

For another exporter/version, record what actually changed and freeze the smallest
adaptation before testing a separate input. Do not count these synthetic examples
as a real firmware-version or sensor-accuracy validation. Codex or Claude Code can
follow the same saved files and commands; they are optional engineering tools, not
runtime dependencies. Share only material authorized for the selected provider.

## 5. Failures, limits and what to retain

Try a copy of `conversion.json` with `latitude_field` changed to the absent
`other_latitude`: conversion must exit 1 and create no output. A profile declaring
`altitude_reference: "ellipsoid"` must also exit 1 rather than treating it as MSL.
Use a different new output path for each test. Neither failure should change the
successful inputs or profiles.

| CLI outcome | Meaning and next step |
|---|---|
| Conversion exits 0 | New JSONL created; proceed to SPI ingestion and check all outcomes. |
| Conversion exits 1 | Wrong profile, unsupported/malformed input, limits or output I/O may be responsible. It prints the fixed message `position csv conversion failed; verify input, profile and a new output path`; inspect the local input/specification and choose a new output path. |
| SPI exits 0 | Report produced with no rejected selected records; inspect mapped and retained values, not just the exit code. |
| SPI exits nonzero | A malformed profile/input produces an error; partial rejection produces an indexed report. Keep the complete report and resolve the affected records. |

Wrong converter argument count prints usage and exits 1. Validation happens before
the new output is opened, and existing converter output files are never overwritten.
An OS write failure can leave a partial new file. Shell redirection for the SPI can
also create or overwrite a report even when the command fails: always use new paths
and inspect exit status. File existence alone is not success.

Conversion bounds are **16 MiB for input and total output**, **64 KiB per physical
input line**, **63 KiB per generated JSONL record**, and **4,096 bytes per profile**.
The generated-record limit leaves 1 KiB headroom for downstream numeric serialization.
Counters are unsigned decimal integers and must fit `u64` after unit conversion.
Selected numbers must be finite; latitude/longitude must be within ±90/±180 degrees.
Malformed UTF-8/CSV, NULs, blank/ragged rows and duplicate/blank headers are refused.
Valid quoted CSV cells are supported. The [JSONL reader limits](../crates/jsonl-log/README.md)
and [adapter contract](../crates/adapter-spi/README.md) also apply.

`source_record`, profiles, report indices and counters are **unsealed sidecars**.
Envelope digests cover mapped common-model and claim content, not every retained
unknown field or the complete report. Digests are not signatures, source authentication
or proof of measurement quality. Preserve originals, settings and full results under
your data-handling policy; do not post recordings or diagnostic output publicly by default.

## Optional separate Blackbox GPS stream

If starting from a supported recorded Blackbox input, the existing
[external-decoder stage](recorded-blackbox.md) can select separate G records:

```sh
python3 scripts/decode_blackbox_recording.py /path/to/trusted/blackbox_decode recording.bfl new-gps-output --stream gps
```

GPS mode's structural header gate accepts Betaflight 4.2.x or 4.3.x; default main mode
remains restricted to 4.2.x. This is not verified support for every firmware revision
or device: check the exact recording and decoder version. The wrapper requires one
complete log and checks selected GPS-frame accounting. It selects `decoded.csv`;
accompanying main/event/GPX files remain unnormalized sidecars, not additional mapped
positions. Read `decode.json` and the exact CSV header before declaring conversion
fields/units. Do not reuse this example's invented field names blindly.

No decoder executable, third-party recording or raw fixture is bundled. Choose a
trusted, separately installed decoder and check its license; the wrapper is not its
sandbox. Decoder success alone is not common-model normalization. Keep actual input
validation and synthetic recipe results separate.
