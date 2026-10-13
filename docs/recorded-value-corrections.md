# Recorded values, declared units and selected clocks

Saved data remains a producer's report. These readers do not establish device identity,
physical state, calibrated measurements or the relationship between separate clocks.

- KML features and tracks with explicit timestamps retain those timestamps in common
  observations. Untimed geometry remains untimed; malformed or ambiguous time is refused.
- Robotnik battery declarations distinguish summary and cell rows, minutes converted to
  seconds, and charger connection from actual charge flow. Use the bundled
  `profiles/declared/electrical-schema-reuse/json-profile.toml` declaration.
- The optional external Blackbox decoder uses its supported SI-unit arguments. Tests use
  authored input and a mock executable; no external decoder or device validation is implied.
- Engineering-unit scaling refuses a nonzero value that would silently underflow to zero.
  Genuine zero, identity conversions and valid offset conversions retain their behavior.
- Saved candump ordering is checked within the selected interface. Other interfaces are
  still parsed, range checked and accounted for; rows are not sorted or repaired.

The `musubi-reference-readers` library also provides explicit reported-unit reading for
DataFlash UNIT/MULT declarations and handset columns. Default raw reads remain available.
`ArduPilotBinReader::read_report_with_units` retains original values and reports unit
qualification. `EdgeTxCsvReader::read_with_reported_units` does not interpret an ambiguous
`rpm` header as a frequency: it retains the original cell and emits an unqualified result.
Declared handset writer tables are an explicit opt-in, distinct from interpreting header text.
Streaming handset reads retain bounded lines and report emitted-record counts on failure.

These are library entry points; they do not automatically enable unit conversion in every
CLI or viewer path. The ordinary viewer input and export formats remain unchanged.
The authored regression examples are in `crates/reference-readers/tests/bin_reported_units.rs`,
`handset_reported_units.rs`, and `tests/test_public_priority1.py`.
