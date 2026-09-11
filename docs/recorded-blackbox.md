# Offline raw-recording to CSV stage

`scripts/decode_blackbox_recording.py` is an optional Python 3 command-line stage.
It invokes an operator-selected, independently installed `blackbox_decode`
executable and writes native-unit CSV for the existing `musubi-decoded-csv`
library. It does not download a decoder, connect to a device, or normalize
observations into the core object model.

## Input contract

The structural gate requires one raw Blackbox recording with a header declaring
Betaflight 4.2.x and a complete End of log event. This is a format/profile
restriction, not verified support for every firmware revision or device.
CSV, INAV headers, other revision families, concatenated logs and missing
terminators are refused. Header checks do not prove that the binary body is valid;
decoding and later CSV parsing can still fail.

The decoder must accept `--stdout --index 1 --unit-vbat raw --unit-amperage raw
--unit-flags raw` and emit its main-frame count and corruption summary to stderr.
The wrapper relies on that text format. A different decoder/version needs its own
review and test; matching text is not proof of a correct decoder.
Use a trusted executable: this wrapper is not a sandbox for third-party code.
The decoder and its license remain the operator's separate responsibility.
No third-party executable, source or recording is included.

## Run

Choose a new output directory; an existing directory is never overwritten:

```sh
python3 scripts/decode_blackbox_recording.py /path/to/blackbox_decode recording.bfl new-output
```

On success the directory contains:

- `decoded.csv`: main frames with merged slow fields.
- `decode.json`: input/decoder SHA-256, counts, native units and unresolved domain.
- `decoder.stderr.log`: the decoder's diagnostics.

A nonzero decoder result, reported corruption, absent/zero main count, CSV row
count mismatch or timeout fails the command. A failure may leave the new
directory and diagnostic log; inspect it and choose another new directory when
retrying. Do not treat the presence of a directory as success.

These output files may contain identifying data from your input or decoder.
Keep them under your data-handling policy; do not attach them to a public issue
without reviewing their contents.

## Read the CSV

The script stops at CSV: `common_output: NOT_RUN` is intentional.
In a Rust consumer using the published library:

```rust,ignore
let bytes = std::fs::read("new-output/decoded.csv")?;
let rows = musubi_decoded_csv::parse(&bytes, "time")?;
```

The parser retains columns and rejects malformed decoded CSV. It does not
apply the sidecar units automatically. This stage declares time in microseconds,
`vbatLatest` in cV and `amperageLatest` in cA; keep these labels with the values
or convert explicitly in your consumer. Do not infer absolute time or vehicle
mobility from a firmware name.

GPS/events are not normalized by this stage. Retain the original recording if
you need them. Missing sampled loop iterations and recorded main frames are
different counts. The stage does not claim complete binary-field coverage,
automatic version adaptation, live operation or device compatibility.

## Tests

```sh
python3 -m unittest discover -s tests -p test_blackbox_recording.py
cargo test -p musubi-decoded-csv
```

Python tests use authored synthetic bytes and a mocked decoder; they do not
exercise an installed third-party decoder or a real recording.
