# Recorded pose text to a reusable CSV table

Use this offline stage for whitespace-separated records with exactly these eight
numeric fields, in order:

```text
timestamp_unix_s tx ty tz qx qy qz qw
```

This is a format contract, not a header to include in the input. The caller must
establish that the timestamp is Unix seconds, translation is meters in the source
frame, and the quaternion is ordered x/y/z/w. The converter does not establish
those meanings from numbers. It does not normalize quaternions, change frames,
infer geographic coordinates, or infer clock quality or platform identity.

## Reproduce from this repository alone

You need Python 3 (standard library only) and the Rust toolchain pinned in
`rust-toolchain.toml`. No service, provider account, or separate repository is needed.
Save this authored synthetic example as `pose.txt`:

```text
# synthetic position and quaternion records
1.000001 1.25 2 3 0 0 0 1
1.000001 1.5 2 3 0 0 0 1
1.000002 1.75 2 3 0 0 0 1
```

From the repository root, use new output filenames:

```sh
python3 scripts/convert_pose_text.py pose.txt converted.csv --report accounting.json --allow-equal-time
cargo run --quiet -p musubi-decoded-csv --example inspect_pose_csv -- --allow-equal-time converted.csv > checked.csv
cmp converted.csv checked.csv
python3 -m unittest discover -s tests -p test_pose_text_conversion.py
cargo test -p musubi-decoded-csv
```

The report contains `data_rows: 3`, `comment_lines: 1`, `blank_lines: 0`, and
`equal_time_pairs: 1`, plus the original comment text. CSV contains three rows and
nine columns, including the original seconds string and the prepended
`record_time (us)` values `1000001`, `1000001`, `1000002`.
The example validates the entire CSV before emitting its original bytes. A parse
failure returns nonzero and emits no CSV. Shell redirection can still create an
empty destination file; always check the exit status before using it.

For a second independent synthetic input, save the following as `pose-extra.txt`:

```text
# another exporter with a declared numeric extension
2.000001 -1 2 3 0 0 0 2 0.25
2.000002 -2 2 3 0 0 0 2 0.50
```

```sh
python3 scripts/convert_pose_text.py pose-extra.txt extra.csv --report extra-accounting.json --extra-column source_score
cargo run --quiet -p musubi-decoded-csv --example inspect_pose_csv -- extra.csv > extra-checked.csv
cmp extra.csv extra-checked.csv
```

This produces two rows and ten columns. `source_score` and its values are retained;
the quaternion's `qw` value remains `2`. Repeat `--extra-column NAME` in input-column
order for further numeric extensions. No parser or core change is needed for them.

## Ordering and preservation

Strictly increasing timestamps remain the default. Equal timestamps are accepted
only with `allow_equal_time=True` in the Python API / `--allow-equal-time` in the
pose CLI and `parse_non_decreasing` in Rust. Opt in at both stages when needed.
Equal-time records remain in source order; they do not establish causal order.
Neither path sorts, drops duplicates, or fabricates distinct timestamps. Decreasing
timestamps fail even with the option. Resolve the input's semantics instead of
sorting a recording merely to make it pass.

The converter retains numeric spellings as CSV cells and keeps whole-line comments
in the required accounting report. Blank lines are counted. Whitespace separators
are regenerated: keep the original input for byte-level provenance. The Rust table
trims cells and types finite numbers, so its values are not a byte-preserving archive.
The historical `boot_us` field name does not make these absolute timestamps a boot
clock. Consumers must explicitly preserve the declared time basis and unknown trust.

## Rejections and limits

Empty/comment-only input, missing or unnamed extra fields, inline comments,
nonnumeric/nonfinite values, negative times, decreasing times, and fractional
microseconds are rejected. Times must fit nonnegative signed 64-bit microseconds;
there is no rounding. Extension names must be unique, nonreserved identifiers.
Raw binary, JSON, quoted CSV, and other pose layouts require another conversion path.

The converter validates before writing and refuses existing output/report files.
The two files are not a transaction: a later filesystem failure can leave one file
behind. Use fresh paths and check errors. Inputs and outputs are held in memory;
this is a bounded-file tool, not live streaming. Reports retain source comments and
may therefore be sensitive; keep recordings and reports out of public issues.

This public stage ends at CSV/table parsing, not a common-Observation adapter,
robot connection, calibrated pose estimate, or device/version support guarantee.
Bundled tests and examples are synthetic. No third-party recording or decoder is
distributed, and no external dependency or license change is introduced.
