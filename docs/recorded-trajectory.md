# Recorded CSV time-column conversion

Use `scripts/convert_trajectory_csv.py` when a recorded export has a known seconds
column and the consuming CSV reader requires integer microseconds. Python 3 uses
only its standard library. Nothing connects to a device or downloads another tool.

## Try a synthetic input

Create `input.csv` with this authored example:

```csv
elapsed (s),position (m),unrecognized,uncertainty
0.000001,1.25,ready,
0.000002,1.5,new_value,
```

Run from this repository:

```sh
python3 scripts/convert_trajectory_csv.py input.csv converted.csv --time-column 'elapsed (s)'
cargo run -p musubi-decoded-csv --example inspect -- converted.csv record_time
```

The example reports two rows and five columns. The converter prepends
`record_time (us)` with values `1` and `2`; original columns, values and empty cells
remain. CSV serialization is regenerated, so this is not byte-identical retention.
The Rust parser retains unknown columns and unit suffixes; it trims cell text and
types numeric values. Use its table to implement your own explicit semantic mapping.

## Input contract and failures

- Select the exact header spelling of a seconds column. Its physical meaning is
  an operator assertion, not inferred from the name.
- Times must be finite, nonnegative, strictly increasing and exactly representable
  in integer microseconds within signed 64-bit range. No rounding is performed.
- At least one data row, unique headers and equal row widths are required. A
  generated `record_time` column must not overwrite an existing source column.
- Headers and cells containing commas, quotes or embedded newlines are refused:
  the consuming reader is deliberately not a general quoted-CSV implementation.
- The converter validates its contract before opening the output, and refuses to
  overwrite an existing output. The consuming Rust parser performs its own further
  checks, including normalized header uniqueness, valid unit suffixes and finite
  numeric values. Conversion success alone is not downstream parse success.
- Input and output are held in memory. This is for bounded offline files, not
  streaming acquisition. Keep original recordings and outputs in a suitable private
  location; do not attach identifying data to public issues.

## What conversion does not establish

Integer microseconds do not establish a boot clock, UTC, synchronization, platform
identity, units of other fields, or position truth. The existing table API's
`boot_us` member is not evidence of a boot-relative clock for this input; a consumer
must retain unknown clock quality when no clock origin is known. Local-map estimates
must not be relabeled as geographic coordinates or ground truth.

This stage stops at CSV/table parsing. It does not provide a common-Observation
adapter, trajectory reconstruction, firmware decoder, ROS/rosbag support or live
robot integration. It makes no device compatibility claim.

```sh
python3 -m unittest discover -s tests -p test_trajectory_conversion.py
cargo test -p musubi-decoded-csv
```

Tests use authored synthetic inputs only. No third-party recordings or decoder are
bundled, and no additional dependencies or license changes are required.
