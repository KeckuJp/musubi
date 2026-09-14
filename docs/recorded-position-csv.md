# Read position CSV into common observations

The offline `ingest_recorded_csv` example connects the public decoded CSV reader to
the existing geodetic mapper. Each accepted row produces a sealed `PlatformState`
observation. It uses the same position checks, MARK derivation and content digest as
the JSONL adapter; no intermediate JSONL file or private component is required.

Use this route only when you have established that the selected time column is a
boot-relative microsecond counter, latitude/longitude are degrees, and altitude is
metres above mean sea level. A profile declares those meanings; the program cannot
discover whether the declaration is true. It does not convert local coordinates,
ellipsoidal/home-relative heights, decoder-specific integer scales, or wall clocks.
For explicitly scaled inputs, the existing [position converter](adapt-position-csv.md)
and [layout profiles](adapt-px4-position-csv.md) remain available.

## Try it with generated data

Create two self-authored example rows and a profile:

```sh
python3 - <<'PY'
import json, random
from pathlib import Path
rng = random.Random(7)
rows = ['boot (us),latitude (deg),longitude (deg),height (m),extra']
for i in range(2):
    rows.append(f'{100+i},35.{rng.randrange(1000,9999)},139.25,{10+i},nan')
Path('sample.csv').write_text('\n'.join(rows)+'\n')
Path('mapping.json').write_text(json.dumps({
    'message_type':'POSITION', 'latitude_field':'latitude',
    'longitude_field':'longitude', 'altitude_field':'height',
    'position_units':'deg-m-msl'
}))
PY
cargo run --locked -p musubi-adapter-spi --example ingest_recorded_csv -- \
  sample.csv mapping.json sample 1700000000000 boot --preserve-nonfinite-as-text
```

`SOURCE_ID` and the positive `RECEIVED_AT_MS` are caller-supplied identifiers and
receipt time, not facts extracted from the equipment. The example timestamp above is
synthetic. Column names in the profile omit unit suffixes. Explicit suffixes must be
`us` for time, `deg` for latitude/longitude and `m` for altitude. Bare columns rely on
the declared profile. Unrelated columns and their suffixes retain their source meaning.
Changing the column mapping reuses the same reader and normalizer for another layout.

Optional `fix_field`/`min_fix` apply the existing integer fix policy. An optional
`source_channel_path` of `["data", "Channel"]` retains a textual source column label
as a source assertion. It does not identify a physical receiver or validate its fix.
Omitting `domain` yields `Unknown`. No UTC observation time, clock anchor, device
compatibility, coordinate calibration or confidence is inferred. Confidence remains
zero; the receipt time is separate from the exact integer boot counter.

## Rejections, retention and limits

- A structurally valid row is either `mapped` or `rejected`, identified by its
  zero-based record index. Missing/nonfinite position values, insufficient fix and
  invalid channel labels produce indexed INVALID outcomes after parsing succeeds; they are not filled with
  zero or silently removed. A report containing rejected rows exits unsuccessfully.
- Parsing remains strict by default. `--preserve-nonfinite-as-text` retains `nan` and
  `inf` as text in uninterpreted columns; selected position values then reject by row.
  Without that option a nonfinite token rejects the whole CSV during strict parsing.
  `--allow-equal-time` preserves ties in source order. This is not causal event order.
  Decreasing time, invalid time, quotes, duplicate names and ragged rows reject the
  whole input before a report is emitted.
- The report keeps the original UTF-8 CSV once in `source_csv`, including lexical
  spelling and unknown columns. The library also exposes the parsed `Table`.
  The original CSV, boot counter and diagnostic report are **not sealed by the
  observation digest**. The digest covers the existing common object and claim only.
- This is an in-memory, bounded file reader, not a streaming log processor. Maximums
  are 16 MiB input, 64 KiB per physical line, 100,000 data rows and 4 KiB profile.
  The CLI additionally refuses a diagnostic report larger than 128 MiB. Large files
  require deliberate splitting with record accounting; limits are not truncation.

The JSONL batch reader now passes its already parsed records to the same mapper,
avoiding repeated serialization and parsing for every observation. Its original input
bounds and the public envelope semantics remain in force.

These checks use authored synthetic inputs. They establish the declared conversion
path, not live equipment, every exporter/firmware version, field use or human UX fit.
