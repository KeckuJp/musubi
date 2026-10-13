use musubi_reference_types::{ChannelId, ClockBasis, FamilyProfile};
use std::collections::BTreeSet;
use std::io::BufRead;

#[path = "handset_controls.rs"]
mod handset_controls;
pub use handset_controls::{HandsetControlKind, HandsetWriter};

use crate::{
    FieldValue, Observation, ProfileReader, ReadError, default_time_confidence, observation,
};

/// handset CSV reader。
#[derive(Debug, Clone, Copy, Default)]
pub struct EdgeTxCsvReader;

/// Caller-qualified fixed-writer sensor role; never inferred from a custom label.
#[derive(Debug, Clone, Copy)]
pub enum HandsetCompoundKind {
    GpsPair,
    SensorCalendar,
}

fn compound_fields(
    mut row: Observation,
    selected: &[(&str, HandsetCompoundKind)],
) -> Result<Observation, ReadError> {
    if selected.len() > 32
        || selected
            .iter()
            .map(|(name, _)| name)
            .collect::<BTreeSet<_>>()
            .len()
            != selected.len()
        || selected.iter().any(|(name, _)| name.is_empty())
        || row
            .fields
            .iter()
            .any(|(name, _)| name.starts_with("handset_reported."))
    {
        return Err(ReadError::Profile(
            "invalid or colliding handset compound selection".into(),
        ));
    }
    let mut added = Vec::new();
    for (name, kind) in selected {
        let raw = &row
            .fields
            .iter()
            .find(|(key, _)| key == name)
            .ok_or_else(|| ReadError::Profile("selected handset compound column absent".into()))?
            .1;
        let prefix = format!("handset_reported.{name}");
        let mut values = Vec::new();
        let disposition = match (kind, raw) {
            (_, FieldValue::Blank) => "BLANK_REPORTED",
            (HandsetCompoundKind::GpsPair, FieldValue::Text(text)) => {
                let parts = text.split(' ').collect::<Vec<_>>();
                let coordinate = |s: &str| -> Option<f64> {
                    let body = s.strip_prefix('-').unwrap_or(s);
                    let (whole, fraction) = body.split_once('.')?;
                    if whole.is_empty()
                        || fraction.len() != 6
                        || !whole
                            .bytes()
                            .chain(fraction.bytes())
                            .all(|b| b.is_ascii_digit())
                    {
                        return None;
                    }
                    s.parse::<f64>().ok().filter(|n| n.is_finite())
                };
                match parts.as_slice() {
                    [lat, lon] => match (coordinate(lat), coordinate(lon)) {
                        (Some(lat), Some(lon)) if lat.abs() <= 90.0 && lon.abs() <= 180.0 => {
                            if lat == 0.0 || lon == 0.0 {
                                "ZERO_PAIR_NOT_EMITTED_BY_FIXED_WRITER"
                            } else {
                                values.push(("latitude_deg", FieldValue::F64(lat)));
                                values.push(("longitude_deg", FieldValue::F64(lon)));
                                "REPORTED_COORDINATES_DATUM_UNQUALIFIED"
                            }
                        }
                        _ => "INVALID_COORDINATE_PAIR",
                    },
                    _ => "INVALID_COORDINATE_PAIR",
                }
            }
            (HandsetCompoundKind::SensorCalendar, FieldValue::Text(text)) => {
                if text.len() == 19
                    && text.is_ascii()
                    && text.as_bytes()[10] == b' '
                    && text.as_bytes()[4] == b'-'
                    && text.as_bytes()[7] == b'-'
                    && text.as_bytes()[13] == b':'
                    && text.as_bytes()[16] == b':'
                    && parse_date_time(&text[..10], &text[11..]).is_some()
                {
                    for (key, start, end) in [
                        ("year", 0, 4),
                        ("month", 5, 7),
                        ("day", 8, 10),
                        ("hour", 11, 13),
                        ("minute", 14, 16),
                        ("second", 17, 19),
                    ] {
                        values.push((
                            key,
                            FieldValue::I64(text[start..end].parse().map_err(|_| {
                                ReadError::Profile("invalid fixed calendar layout".into())
                            })?),
                        ));
                    }
                    "REPORTED_CALENDAR_TIMEZONE_UNQUALIFIED"
                } else {
                    "INVALID_SENSOR_CALENDAR"
                }
            }
            _ => "UNQUALIFIED_COMPOUND_REPRESENTATION",
        };
        added.push((
            format!("{prefix}.disposition"),
            FieldValue::Text(disposition.into()),
        ));
        added.push((
            format!("{prefix}.basis"),
            FieldValue::Text(
                "CALLER_SELECTED_FIXED_WRITER_SENSOR_ROLE_NOT_PHYSICAL_VALIDITY".into(),
            ),
        ));
        for (key, value) in values {
            added.push((format!("{prefix}.{key}"), value));
        }
    }
    row.fields.extend(added);
    Ok(row)
}

fn scale_for(text: &str) -> Option<(&'static str, f64, f64)> {
    Some(match text {
        "V" => ("V", 1.0, 0.0),
        "A" => ("A", 1.0, 0.0),
        "mA" => ("A", 0.001, 0.0),
        "W" => ("W", 1.0, 0.0),
        "mW" => ("W", 0.001, 0.0),
        "kts" => ("m/s", 1852.0 / 3600.0, 0.0),
        "m/s" => ("m/s", 1.0, 0.0),
        "f/s" => ("m/s", 0.3048, 0.0),
        "kmh" => ("m/s", 1.0 / 3.6, 0.0),
        "mph" => ("m/s", 0.44704, 0.0),
        "m" => ("m", 1.0, 0.0),
        "ft" => ("m", 0.3048, 0.0),
        "km" => ("m", 1000.0, 0.0),
        "°C" | "@C" => ("K", 1.0, 273.15),
        "°F" | "@F" => ("K", 5.0 / 9.0, 459.67),
        "%" => ("ratio", 0.01, 0.0),
        "mAh" => ("C", 3.6, 0.0),
        "°" | "@" => ("rad", std::f64::consts::PI / 180.0, 0.0),
        "rad" => ("rad", 1.0, 0.0),
        "ml" => ("m3", 0.000001, 0.0),
        "mlm" => ("m3/s", 0.000001 / 60.0, 0.0),
        "Hz" => ("Hz", 1.0, 0.0),
        "ms" | "mS" => ("s", 0.001, 0.0),
        "us" | "uS" => ("s", 0.000001, 0.0),
        "dB" => ("dB", 1.0, 0.0),
        "dBm" | "dbm" => ("dBm", 1.0, 0.0),
        "g" => ("g_acceleration_reported", 1.0, 0.0),
        "fOz" => ("fOz_reported_volume", 1.0, 0.0),
        _ => return None,
    })
}

/// Writer unit-table meaning, independent of its spelling in one build.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum UnitMeaning {
    Text(&'static str),
    RotationPerMinute,
}

/// Caller-declared fixed writer and build language whose `STR_VTELEMUNIT` table produced the
/// header. Never inferred from a log; a header does not record its build language.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HandsetUnitTable {
    /// EdgeTX 65bb97db `translations/i18n/en.h`.
    EdgeTx65bb97dbEnglish,
    /// EdgeTX 65bb97db `translations/i18n/tw.h`: UNIT_RPMS and UNIT_G are both "rpm".
    EdgeTx65bb97dbTw,
    /// OpenTX fb8622dd `translations/en.h.txt`.
    OpenTxFb8622ddEnglish,
}

impl HandsetUnitTable {
    fn id(self) -> &'static str {
        match self {
            Self::EdgeTx65bb97dbEnglish => "EDGETX_65BB97DB_EN",
            Self::EdgeTx65bb97dbTw => "EDGETX_65BB97DB_TW",
            Self::OpenTxFb8622ddEnglish => "OPENTX_FB8622DD_EN",
        }
    }

    /// Header texts for UNIT_VOLTS..UNIT_DBM, in writer enum order, after 3-byte truncation.
    fn entries(self) -> [(&'static str, UnitMeaning); 29] {
        use UnitMeaning::{RotationPerMinute as Rpm, Text as T};
        // The tw build spells UNIT_G "rpm": the text differs, the meaning stays g.
        let (g, deg_c, deg_f, deg, ms, us, dbm) = match self {
            Self::EdgeTx65bb97dbEnglish => ("g", "°C", "°F", "°", "ms", "us", "dBm"),
            Self::EdgeTx65bb97dbTw => ("rpm", "°C", "°F", "°", "mS", "uS", "dBm"),
            Self::OpenTxFb8622ddEnglish => ("g", "@C", "@F", "@", "mS", "uS", "dbm"),
        };
        [
            ("V", T("V")),
            ("A", T("A")),
            ("mA", T("mA")),
            ("kts", T("kts")),
            ("m/s", T("m/s")),
            ("f/s", T("f/s")),
            ("kmh", T("kmh")),
            ("mph", T("mph")),
            ("m", T("m")),
            ("ft", T("ft")),
            (deg_c, T(deg_c)),
            (deg_f, T(deg_f)),
            ("%", T("%")),
            ("mAh", T("mAh")),
            ("W", T("W")),
            ("mW", T("mW")),
            ("dB", T("dB")),
            ("rpm", Rpm),
            (g, T("g")),
            (deg, T(deg)),
            ("rad", T("rad")),
            ("ml", T("ml")),
            ("fOz", T("fOz")),
            ("mlm", T("mlm")),
            ("Hz", T("Hz")),
            (ms, T(ms)),
            (us, T(us)),
            ("km", T("km")),
            (dbm, T(dbm)),
        ]
    }
}

/// The writer appends `(unit)` after a label; a label holding its own parenthesis is unattributable.
fn header_unit(key: &str) -> Option<(&str, bool)> {
    let body = key.strip_suffix(')')?;
    let (label, text) = body.rsplit_once('(')?;
    Some((text, label.contains(['(', ')'])))
}

fn scaled(raw: &FieldValue, unit: &str, factor: f64, offset: f64) -> (FieldValue, &'static str) {
    let numeric = match raw {
        FieldValue::I64(n) if n.unsigned_abs() <= (1_u64 << 53) => Some(*n as f64),
        FieldValue::F64(n) if n.is_finite() => Some(*n),
        _ => None,
    };
    // Temperature offsets are in the input unit; preserve the exact zero-K boundary.
    let converted = numeric.map(|n| {
        if unit == "K" {
            (n + offset) * factor
        } else {
            n * factor
        }
    });
    match converted {
        Some(n) if unit == "K" && n < 0.0 => (FieldValue::Blank, "BELOW_ABSOLUTE_ZERO"),
        Some(n) if n.is_finite() && (unit == "K" || numeric == Some(0.0) || n != 0.0) => {
            (FieldValue::F64(n), "REPORTED_VALUE")
        }
        Some(_) => (FieldValue::Blank, "DERIVED_NONFINITE_OR_UNDERFLOW"),
        None if matches!(raw, FieldValue::Blank) => (FieldValue::Blank, "BLANK"),
        None => (FieldValue::Blank, "NONNUMERIC_OR_PRECISION_UNQUALIFIED"),
    }
}

fn push_si(
    added: &mut Vec<(String, FieldValue)>,
    key: &str,
    (value, disposition): (FieldValue, &str),
    unit: &str,
    basis: &str,
) {
    for (suffix, value) in [
        ("value", value),
        ("unit", FieldValue::Text(unit.into())),
        ("disposition", FieldValue::Text(disposition.into())),
        ("basis", FieldValue::Text(basis.into())),
    ] {
        added.push((format!("handset_si.{key}.{suffix}"), value));
    }
}

fn reject_reserved(row: &Observation) -> Result<(), ReadError> {
    if row
        .fields
        .iter()
        .any(|(key, _)| key.starts_with("handset_si."))
    {
        return Err(ReadError::Profile(
            "reserved handset_si source column".into(),
        ));
    }
    Ok(())
}

const HEADER_BASIS: &str = "REPORTED_HEADER_UNIT_VALIDITY_UNQUALIFIED";

fn reported_units(mut row: Observation) -> Result<Observation, ReadError> {
    reject_reserved(&row)?;
    let mut added = Vec::new();
    for (key, raw) in &row.fields {
        let Some((text, label_parenthesis)) = header_unit(key) else {
            continue;
        };
        if label_parenthesis && (scale_for(text).is_some() || text == "rpm") {
            let withheld = (FieldValue::Blank, "LABEL_PARENTHESIS_UNIT_UNATTRIBUTABLE");
            push_si(&mut added, key, withheld, "UNQUALIFIED", HEADER_BASIS);
            continue;
        }
        if text == "rpm" {
            // EdgeTX 65bb97db i18n/tw.h writes UNIT_G as "rpm" too, so the header alone does not
            // name one dimension; the raw cell stays and only a declared table can resolve it.
            let withheld = (FieldValue::Blank, "WRITER_UNIT_TEXT_SHARED_BY_RPM_AND_G");
            push_si(&mut added, key, withheld, "UNQUALIFIED", HEADER_BASIS);
            continue;
        }
        let Some((unit, factor, offset)) = scale_for(text) else {
            continue;
        };
        push_si(
            &mut added,
            key,
            scaled(raw, unit, factor, offset),
            unit,
            HEADER_BASIS,
        );
    }
    row.fields.extend(added);
    Ok(row)
}

/// Units for caller-attested writer unit columns under one declared writer/language table.
/// A text absent from that table contradicts the declaration and refuses; a text the table
/// gives two meanings stays explicit and unconverted.
fn declared_units(
    mut row: Observation,
    table: HandsetUnitTable,
    columns: &[&str],
) -> Result<Observation, ReadError> {
    reject_reserved(&row)?;
    if columns.is_empty()
        || columns.len() > 64
        || columns.iter().collect::<BTreeSet<_>>().len() != columns.len()
    {
        return Err(ReadError::Profile(
            "invalid declared handset unit column selection".into(),
        ));
    }
    let basis = format!(
        "CALLER_DECLARED_WRITER_TABLE_{}_AND_UNIT_COLUMN",
        table.id()
    );
    let entries = table.entries();
    let mut added = Vec::new();
    for column in columns {
        let raw = &row
            .fields
            .iter()
            .find(|(key, _)| key == column)
            .ok_or_else(|| ReadError::Profile("declared handset unit column absent".into()))?
            .1;
        let (text, _) = header_unit(column).ok_or_else(|| {
            ReadError::Profile("declared handset unit column has no writer unit suffix".into())
        })?;
        let meanings = entries
            .iter()
            .filter(|(spelling, _)| *spelling == text)
            .map(|(_, meaning)| *meaning)
            .collect::<BTreeSet<_>>();
        let mut meanings = meanings.into_iter();
        match (meanings.next(), meanings.next()) {
            (None, _) => {
                return Err(ReadError::Profile(
                    "unit text contradicts the declared handset writer table".into(),
                ));
            }
            (Some(_), Some(_)) => {
                let withheld = (FieldValue::Blank, "DECLARED_TABLE_TEXT_HAS_TWO_MEANINGS");
                push_si(&mut added, column, withheld, "UNQUALIFIED", &basis);
            }
            (Some(UnitMeaning::RotationPerMinute), None) => {
                push_si(
                    &mut added,
                    column,
                    scaled(raw, "Hz", 1.0 / 60.0, 0.0),
                    "Hz",
                    &basis,
                );
            }
            (Some(UnitMeaning::Text(spelling)), None) => {
                let (unit, factor, offset) = scale_for(spelling).ok_or_else(|| {
                    ReadError::Profile("declared writer table entry has no scale".into())
                })?;
                push_si(
                    &mut added,
                    column,
                    scaled(raw, unit, factor, offset),
                    unit,
                    &basis,
                );
            }
        }
    }
    row.fields.extend(added);
    Ok(row)
}

impl EdgeTxCsvReader {
    /// Interpret explicitly selected historical controls, never produce control commands.
    pub fn read_with_reported_controls(
        &self,
        profile: &FamilyProfile,
        input: &[u8],
        writer: HandsetWriter,
        selected: &[(&str, HandsetControlKind)],
    ) -> Result<Vec<Observation>, ReadError> {
        self.read(profile, input)?
            .into_iter()
            .map(|row| handset_controls::enrich(row, writer, selected))
            .collect()
    }

    /// Preserve native streaming bounds and acknowledge only successfully adapted records.
    pub fn read_stream_with_reported_controls<
        R: BufRead,
        F: FnMut(Observation) -> Result<(), String>,
    >(
        &self,
        profile: &FamilyProfile,
        input: &mut R,
        writer: HandsetWriter,
        selected: &[(&str, HandsetControlKind)],
        mut emit: F,
    ) -> Result<HandsetStreamCounts, HandsetStreamError> {
        self.read_stream(profile, input, |row| {
            emit(handset_controls::enrich(row, writer, selected).map_err(|e| format!("{e:?}"))?)
        })
    }

    /// Combined scalar and explicitly selected compound sensor meanings; defaults stay unchanged.
    pub fn read_with_reported_fields(
        &self,
        profile: &FamilyProfile,
        input: &[u8],
        selected: &[(&str, HandsetCompoundKind)],
    ) -> Result<Vec<Observation>, ReadError> {
        self.read_with_reported_units(profile, input)?
            .into_iter()
            .map(|row| compound_fields(row, selected))
            .collect()
    }

    /// Reuses bounded native streaming and callback acknowledgement, without collecting the file.
    pub fn read_stream_with_reported_fields<
        R: BufRead,
        F: FnMut(Observation) -> Result<(), String>,
    >(
        &self,
        profile: &FamilyProfile,
        input: &mut R,
        selected: &[(&str, HandsetCompoundKind)],
        mut emit: F,
    ) -> Result<HandsetStreamCounts, HandsetStreamError> {
        self.read_stream_with_reported_units(profile, input, |row| {
            emit(compound_fields(row, selected).map_err(|error| format!("{error:?}"))?)
        })
    }
    /// Opt-in fixed-writer scalar header quantities, alongside every original column.
    /// Decimal precision was already applied by the writer. Zero is not proof of sensor validity.
    pub fn read_with_reported_units(
        &self,
        profile: &FamilyProfile,
        input: &[u8],
    ) -> Result<Vec<Observation>, ReadError> {
        self.read(profile, input)?
            .into_iter()
            .map(reported_units)
            .collect()
    }

    /// Units for caller-attested columns under an explicitly declared writer/language table.
    /// Unselected columns get nothing; native fields are unchanged.
    pub fn read_with_declared_units(
        &self,
        profile: &FamilyProfile,
        input: &[u8],
        table: HandsetUnitTable,
        columns: &[&str],
    ) -> Result<Vec<Observation>, ReadError> {
        self.read(profile, input)?
            .into_iter()
            .map(|row| declared_units(row, table, columns))
            .collect()
    }

    /// Bounded native stream variant of [`Self::read_with_declared_units`].
    pub fn read_stream_with_declared_units<
        R: BufRead,
        F: FnMut(Observation) -> Result<(), String>,
    >(
        &self,
        profile: &FamilyProfile,
        input: &mut R,
        table: HandsetUnitTable,
        columns: &[&str],
        mut emit: F,
    ) -> Result<HandsetStreamCounts, HandsetStreamError> {
        self.read_stream(profile, input, |row| {
            emit(declared_units(row, table, columns).map_err(|error| format!("{error:?}"))?)
        })
    }

    /// Reuses native bounded streaming and acknowledgement accounting; no total-file buffer.
    /// A reserved-column failure is reported as an output adaptation failure before acknowledgement.
    pub fn read_stream_with_reported_units<
        R: BufRead,
        F: FnMut(Observation) -> Result<(), String>,
    >(
        &self,
        profile: &FamilyProfile,
        input: &mut R,
        mut emit: F,
    ) -> Result<HandsetStreamCounts, HandsetStreamError> {
        self.read_stream(profile, input, |row| {
            emit(reported_units(row).map_err(|error| format!("{error:?}"))?)
        })
    }
}

/// Opt-in stream line bound, including LF or CRLF. No total input byte bound.
pub const HANDSET_STREAM_LINE_BYTES: usize = 64 * 1024;

/// Consumed bytes, complete data frames, and callback-acknowledged Observations.
/// A malformed complete frame counts as read; an oversized/incomplete I/O prefix does not.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct HandsetStreamCounts {
    pub bytes_read: u64,
    pub records_read: u64,
    pub emitted_records: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HandsetStreamFailure {
    Input(String),
    Decode(ReadError),
    Output(String),
    Limit(&'static str),
}

/// Failure does not retract prior callbacks. Inspect counts; never treat a prefix as full success.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HandsetStreamError {
    pub counts: HandsetStreamCounts,
    /// One-based physical line, including header at line 1.
    pub line_number: u64,
    pub reason: HandsetStreamFailure,
}

impl std::fmt::Display for HandsetStreamError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "handset stream failed at line {} after {} acknowledged records: {:?}",
            self.line_number, self.counts.emitted_records, self.reason
        )
    }
}

impl std::error::Error for HandsetStreamError {}

fn stream_error(
    counts: HandsetStreamCounts,
    line_number: u64,
    reason: HandsetStreamFailure,
) -> HandsetStreamError {
    HandsetStreamError {
        counts,
        line_number,
        reason,
    }
}

/// Read only through LF or the bound+1 detection byte. `fill_buf` may prefetch in the caller,
/// but only consumed bytes count. Keep partial-byte accounting even when input I/O fails.
fn stream_line<R: BufRead>(
    input: &mut R,
    counts: &mut HandsetStreamCounts,
    line: u64,
) -> Result<Vec<u8>, HandsetStreamError> {
    let mut bytes = Vec::new();
    loop {
        let chunk = match input.fill_buf() {
            Ok(chunk) => chunk,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => {
                return Err(stream_error(
                    *counts,
                    line,
                    HandsetStreamFailure::Input(error.to_string()),
                ));
            }
        };
        if chunk.is_empty() {
            return Ok(bytes);
        }
        let available = chunk.len().min(HANDSET_STREAM_LINE_BYTES + 1 - bytes.len());
        let n = chunk[..available]
            .iter()
            .position(|b| *b == b'\n')
            .map_or(available, |index| index + 1);
        let next = counts.bytes_read.checked_add(n as u64).ok_or_else(|| {
            stream_error(
                *counts,
                line,
                HandsetStreamFailure::Limit("input byte counter overflow"),
            )
        })?;
        let ended = chunk[n - 1] == b'\n';
        bytes.extend_from_slice(&chunk[..n]);
        input.consume(n);
        counts.bytes_read = next;
        if bytes.len() > HANDSET_STREAM_LINE_BYTES {
            return Err(stream_error(
                *counts,
                line,
                HandsetStreamFailure::Limit("handset line exceeds 64KiB"),
            ));
        }
        if ended {
            return Ok(bytes);
        }
    }
}

impl EdgeTxCsvReader {
    /// Opt-in bounded-memory ingestion of saved logs of any total size.
    /// Reuses the native decoder for each header+row; holds only one Observation at a time.
    /// Equal times are preserved; decreasing times require explicitly split sessions.
    ///
    /// # Errors
    /// Stops on input/decoding/output failure with counts. Previous callbacks are NOT rolled
    /// back; a callback error is not acknowledged and may have its own partial side effects.
    /// Callers must bound their output storage. This does not change `ProfileReader::read`.
    pub fn read_stream<R: BufRead, F: FnMut(Observation) -> Result<(), String>>(
        &self,
        profile: &FamilyProfile,
        input: &mut R,
        mut emit: F,
    ) -> Result<HandsetStreamCounts, HandsetStreamError> {
        let mut counts = HandsetStreamCounts::default();
        if profile.format != self.format_id() {
            return Err(stream_error(
                counts,
                1,
                HandsetStreamFailure::Decode(ReadError::Profile(
                    "handset reader requires its own format".into(),
                )),
            ));
        }
        let header = stream_line(input, &mut counts, 1)?;
        let mut line_number = 2_u64;
        let mut previous = None;
        loop {
            let row = stream_line(input, &mut counts, line_number)?;
            if row.is_empty() {
                if counts.records_read == 0 {
                    // Preserve empty/header-only and invalid-header failure semantics.
                    let error = self
                        .read(profile, &header)
                        .err()
                        .unwrap_or(ReadError::Malformed {
                            offset: 0,
                            what: "no handset records".into(),
                        });
                    return Err(stream_error(counts, 1, HandsetStreamFailure::Decode(error)));
                }
                return Ok(counts);
            }
            counts.records_read = counts.records_read.checked_add(1).ok_or_else(|| {
                stream_error(
                    counts,
                    line_number,
                    HandsetStreamFailure::Limit("record counter overflow"),
                )
            })?;
            let mut single = Vec::with_capacity(header.len() + row.len());
            single.extend_from_slice(&header);
            single.extend_from_slice(&row);
            let decoded = self.read(profile, &single).map_err(|error| {
                stream_error(counts, line_number, HandsetStreamFailure::Decode(error))
            })?;
            let mut records = decoded.into_iter();
            let Some(record) = records.next() else {
                return Err(stream_error(
                    counts,
                    line_number,
                    HandsetStreamFailure::Limit("no record in decoded line"),
                ));
            };
            if records.next().is_some() {
                return Err(stream_error(
                    counts,
                    line_number,
                    HandsetStreamFailure::Limit("multiple records in decoded line"),
                ));
            }
            if previous.is_some_and(|time| record.t_ms < time) {
                return Err(stream_error(
                    counts,
                    line_number,
                    HandsetStreamFailure::Decode(ReadError::Malformed {
                        offset: usize::try_from(line_number).unwrap_or(usize::MAX),
                        what: "decreasing handset clock; split sessions explicitly".into(),
                    }),
                ));
            }
            previous = Some(record.t_ms);
            let acknowledged = counts.emitted_records.checked_add(1).ok_or_else(|| {
                stream_error(
                    counts,
                    line_number,
                    HandsetStreamFailure::Limit("emission counter overflow"),
                )
            })?;
            emit(record).map_err(|error| {
                stream_error(counts, line_number, HandsetStreamFailure::Output(error))
            })?;
            counts.emitted_records = acknowledged;
            line_number = line_number.checked_add(1).ok_or_else(|| {
                stream_error(
                    counts,
                    line_number,
                    HandsetStreamFailure::Limit("line counter overflow"),
                )
            })?;
        }
    }
}

/// Fixed logs.cpp UNIT_TEXT uses one unescaped quote pair, not general RFC4180.
fn row_cells(line: &str) -> Option<Vec<&str>> {
    let mut cells = Vec::new();
    let mut rest = line;
    loop {
        let end = if let Some(text) = rest.strip_prefix('"') {
            let end = text.find('"')? + 2;
            if rest.len() > end && rest.as_bytes()[end] != b',' {
                return None;
            }
            end
        } else {
            let end = rest.find(',').unwrap_or(rest.len());
            if rest[..end].contains('"') {
                return None;
            }
            end
        };
        cells.push(&rest[..end]);
        if end == rest.len() {
            return Some(cells);
        }
        rest = &rest[end + 1..];
    }
}

fn parse_value(s: &str) -> FieldValue {
    if let Some(text) = s.strip_prefix('"').and_then(|text| text.strip_suffix('"')) {
        return FieldValue::Text(text.to_string());
    }
    let original = s;
    let s = s.trim();
    if s.is_empty() {
        FieldValue::Blank
    } else if let Ok(i) = s.parse::<i64>() {
        FieldValue::I64(i)
    } else if let Ok(f) = s.parse::<f64>() {
        // Match decoded-csv: retain finite integer tokens outside i64 exactly.
        // Non-finite values still reach the reader's existing rejection path.
        if f.is_finite()
            && s.trim_start_matches(['-', '+'])
                .bytes()
                .all(|b| b.is_ascii_digit())
        {
            FieldValue::Text(original.to_string())
        } else {
            FieldValue::F64(f)
        }
    } else {
        FieldValue::Text(original.to_string())
    }
}

fn is_zero(v: &FieldValue) -> bool {
    match v {
        FieldValue::I64(i) => *i == 0,
        FieldValue::F64(f) => *f == 0.0,
        FieldValue::Text(_) | FieldValue::Blank => false,
    }
}

/// Civil calendar -> ordinal ms for local ordering only, never an established UTC instant.
fn parse_date_time(date: &str, time: &str) -> Option<i64> {
    if !date.bytes().all(|b| b.is_ascii_digit() || b == b'-')
        || !time
            .bytes()
            .all(|b| b.is_ascii_digit() || b == b':' || b == b'.')
    {
        return None;
    }
    let mut d = date.split('-').map(|x| x.parse::<i64>().ok());
    let (y, m, day) = (d.next()??, d.next()??, d.next()??);
    if d.next().is_some() || !(1..=9999).contains(&y) || !(1..=12).contains(&m) {
        return None;
    }
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let last_day = match m {
        2 => {
            if leap {
                29
            } else {
                28
            }
        }
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    if !(1..=last_day).contains(&day) {
        return None;
    }
    let mut t = time.split(':');
    let h: i64 = t.next()?.parse().ok()?;
    let mi: i64 = t.next()?.parse().ok()?;
    let sec_str = t.next()?;
    if t.next().is_some() || !(0..24).contains(&h) || !(0..60).contains(&mi) {
        return None;
    }
    let (s, frac) = sec_str.split_once('.').unwrap_or((sec_str, "0"));
    let s: i64 = s.parse().ok()?;
    if !(0..60).contains(&s)
        || frac.is_empty()
        || frac.len() > 9
        || !frac.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let ms: i64 = frac
        .chars()
        .chain("000".chars())
        .take(3)
        .collect::<String>()
        .parse()
        .ok()?;
    Some((days_from_civil(y, m, day) * 86_400 + h * 3600 + mi * 60 + s) * 1000 + ms)
}

/// (y, m, d) → days since 1970-01-01（H. Hinnant）。
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

impl ProfileReader for EdgeTxCsvReader {
    fn format_id(&self) -> &'static str {
        "edgetx_csv"
    }
    fn read(&self, profile: &FamilyProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        if profile.format != self.format_id() {
            return Err(ReadError::Profile(
                "handset reader requires its own format".into(),
            ));
        }
        if bytes.len() > 16 * 1024 * 1024 || bytes.contains(&0) {
            return Err(ReadError::Malformed {
                offset: 0,
                what: "unsupported handset CSV representation or size".into(),
            });
        }
        let text = std::str::from_utf8(bytes).map_err(|e| ReadError::Malformed {
            offset: e.valid_up_to(),
            what: "not utf-8".into(),
        })?;
        let mut lines = text.lines().enumerate();
        let (_, header) = lines.next().ok_or_else(|| ReadError::Malformed {
            offset: 0,
            what: "empty csv".into(),
        })?;
        let cols: Vec<&str> = header.split(',').map(str::trim).collect();
        if cols.iter().any(|c| c.is_empty() || c.contains('"'))
            || cols.iter().collect::<BTreeSet<_>>().len() != cols.len()
        {
            return Err(ReadError::Malformed {
                offset: 0,
                what: "duplicate or empty handset column".into(),
            });
        }
        let col = |name: &str| cols.iter().position(|c| *c == name);
        // 時刻: `Date,Time`（RTC）か `tmr10ms`（RTC 無し build・10 ms tick・boot 相対）。
        // `Date,Time|tmr10ms` は profile が宣言した明示的 OR であり、header に片方だけが完全にある場合に限る。
        enum TimeCol {
            Rtc(usize, usize),
            Tmr10ms(usize),
        }
        let time_col = match profile.fields.time.as_str() {
            "Date,Time" => match (col("Date"), col("Time")) {
                (Some(date), Some(time)) => TimeCol::Rtc(date, time),
                _ => {
                    return Err(ReadError::Malformed {
                        offset: 0,
                        what: "profile requires Date,Time columns".into(),
                    });
                }
            },
            "tmr10ms" | "Time" => match col(&profile.fields.time) {
                Some(index) => TimeCol::Tmr10ms(index),
                None => {
                    return Err(ReadError::Malformed {
                        offset: 0,
                        what: "profile requires tmr10ms column".into(),
                    });
                }
            },
            "Date,Time|tmr10ms" | "Date,Time|Time|tmr10ms" => {
                match (col("Date"), col("Time"), col("tmr10ms")) {
                    (Some(date), Some(time), None) => TimeCol::Rtc(date, time),
                    (None, Some(time), None) if profile.fields.time == "Date,Time|Time|tmr10ms" => {
                        TimeCol::Tmr10ms(time)
                    }
                    (None, None, Some(index)) => TimeCol::Tmr10ms(index),
                    (Some(_), Some(_), Some(_)) => {
                        return Err(ReadError::Malformed {
                            offset: 0,
                            what: "profile time alternatives are ambiguous: both Date,Time and tmr10ms columns are present".into(),
                        });
                    }
                    _ => {
                        return Err(ReadError::Malformed {
                            offset: 0,
                            what: "profile requires exactly one complete time alternative: Date,Time or tmr10ms".into(),
                        });
                    }
                }
            }
            other => {
                return Err(ReadError::Profile(format!(
                    "unsupported explicit handset clock mapping: {other}"
                )));
            }
        };
        let mapped: Vec<(&str, usize)> = profile
            .fields
            .link
            .iter()
            .chain(&profile.fields.battery)
            .chain(&profile.fields.gps)
            .chain(&profile.fields.status)
            .chain(&profile.fields.health)
            .filter_map(|n| col(n).map(|i| (n.as_str(), i)))
            .collect();
        if mapped.is_empty() {
            return Err(ReadError::Profile(
                "no declared handset sensor found".into(),
            ));
        }
        let link_cols: Vec<usize> = profile.fields.link.iter().filter_map(|n| col(n)).collect();
        // GPS 空欄条件は profile の gps 列で判定（列名をハードコードしない＝partner profile で差し替え可）:
        // 「gps 列が全て空欄または 0」（EdgeTX は `GPS` 空欄・`Sats` 0）。gps 列が無い profile は link 全 0 だけで判定。
        let gps_cols: Vec<usize> = profile.fields.gps.iter().filter_map(|n| col(n)).collect();
        let mut out = Vec::new();
        let mut previous = None;
        for (ln, line) in lines {
            let cells = row_cells(line).ok_or_else(|| ReadError::Malformed {
                offset: ln,
                what: "ambiguous handset text quoting".into(),
            })?;
            if cells.len() != cols.len() || line.trim().is_empty() {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "blank or ragged handset row".into(),
                });
            }
            let get = |i: usize| cells.get(i).copied().unwrap_or("");
            if profile
                .fields
                .link
                .iter()
                .chain(&profile.fields.battery)
                .filter_map(|name| col(name))
                .any(|index| get(index).starts_with('"'))
            {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "quoted text cannot replace a selected numeric link/battery value".into(),
                });
            }
            let (t_ms, boot_relative) = match time_col {
                TimeCol::Rtc(di, ti) => (
                    parse_date_time(get(di), get(ti)).ok_or_else(|| ReadError::Malformed {
                        offset: ln,
                        what: format!("bad Date,Time '{},{}'", get(di), get(ti)),
                    })?,
                    false,
                ),
                TimeCol::Tmr10ms(i) => (
                    get(i)
                        .trim()
                        .parse::<u64>()
                        .ok()
                        .and_then(|ticks| ticks.checked_mul(10))
                        .and_then(|ms| i64::try_from(ms).ok())
                        .ok_or_else(|| ReadError::Malformed {
                            offset: ln,
                            what: "bad or out-of-range tmr10ms".into(),
                        })?,
                    true,
                ),
            };
            if previous.is_some_and(|t| t_ms < t) {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "decreasing handset clock; split sessions explicitly".into(),
                });
            }
            previous = Some(t_ms);
            // Keep every source column. Profile selection still controls stale
            // cues, but is not permission to silently discard unknown fields.
            let fields: Vec<(String, FieldValue)> = cols
                .iter()
                .enumerate()
                .map(|(i, n)| ((*n).to_string(), parse_value(get(i))))
                .collect();
            if fields
                .iter()
                .any(|(_, v)| matches!(v, FieldValue::F64(f) if !f.is_finite()))
            {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "nonfinite handset value".into(),
                });
            }
            let link_all_zero =
                !link_cols.is_empty() && link_cols.iter().all(|&i| is_zero(&parse_value(get(i))));
            let gps_blank = gps_cols.iter().all(|&i| {
                let v = parse_value(get(i));
                matches!(v, FieldValue::Blank) || is_zero(&v)
            });
            let stale = link_all_zero && gps_blank;
            let mut o = observation(profile, t_ms, ChannelId::LinkStats, fields, stale);
            // A profile label alone cannot establish the RTC timezone or synchronization.
            o.clock_basis = if boot_relative {
                ClockBasis::BootRelative
            } else {
                ClockBasis::Unknown
            };
            o.time_confidence = default_time_confidence(o.clock_basis);
            o.wall_ms = None;
            out.push(o);
        }
        if out.is_empty() {
            return Err(ReadError::Malformed {
                offset: 0,
                what: "no handset records".into(),
            });
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recorded_text_sensor_preserves_commas_and_text_type_through_existing_reader() {
        let p = crate::profile::parse_profile(TMR_PROFILE, "public").unwrap();
        let reader = crate::reader_for(&p.format).unwrap();
        for (text, expected) in [
            ("\"ready,正常\"", "ready,正常"),
            ("\"0012\"", "0012"),
            ("\"\"", ""),
        ] {
            let input = format!("tmr10ms,RQly(%),Text\n3000,100,{text}\n3000,0,{text}\n");
            let output = reader.read(&p, input.as_bytes()).unwrap();
            assert_eq!(output.len(), 2);
            assert_eq!(output[0].t_ms, 30000);
            assert_eq!(output[0].clock_basis, ClockBasis::BootRelative);
            assert_eq!(output[0].fields[2].1, FieldValue::Text(expected.into()));
            assert_eq!(output[0].fields[2].1.as_f64(), None);
            assert!(!output[0].stale);
            assert!(output[1].stale);
        }
        for row in [
            "3000,100,\"unterminated",
            "3000,100,\"a\"tail",
            "3000,100,a\"b",
            "3000,100,\"a\"\"b\"",
            "3000,100,\"two\nlines\"",
            "\"3000\",100,\"text\"",
            "3000,nan,\"text\"",
            "3000,100,\"ok\",extra",
        ] {
            let input = format!("tmr10ms,RQly(%),Text\n{row}\n");
            assert!(reader.read(&p, input.as_bytes()).is_err(), "{row}");
        }
    }

    #[test]
    fn date_time_round_trips_with_writer_epoch() {
        // 2026-08-31 09:00:00.000 UTC = 1_788_166_800_000 ms（writer 側の t0 と一致）。
        assert_eq!(
            parse_date_time("2026-08-31", "09:00:00.000"),
            Some(1_788_166_800_000)
        );
        assert_eq!(parse_date_time("1970-01-01", "00:00:12.340"), Some(12_340));
        assert_eq!(parse_date_time("2026-8-31", "x"), None);
        assert_eq!(parse_date_time("2026-08-31", "09:00:00.é"), None); // 多バイト小数でも panic しない
    }

    const TMR_PROFILE: &str = r#"
profile_id = "h"
version = "1"
family = "fpv"
source_role = "handset"
format = "edgetx_csv"
extensions = ["csv"]
default_clock_basis = "host_received"
[fields]
time = "tmr10ms"
link = ["RQly(%)"]
"#;

    #[test]
    fn tmr10ms_variant_reads_as_boot_relative_and_missing_time_is_malformed() {
        let p = crate::profile::parse_profile(TMR_PROFILE, "public").expect("profile");
        let obs = EdgeTxCsvReader
            .read(&p, b"tmr10ms,RQly(%)\n3000,100\n3100,0\n")
            .expect("reads");
        assert_eq!(obs.len(), 2);
        assert_eq!(obs[0].t_ms, 30_000);
        assert_eq!(obs[0].clock_basis, ClockBasis::BootRelative);
        assert!(obs[0].wall_ms.is_none());
        assert!(obs[1].stale, "link all zero, no gps column");
        let r = EdgeTxCsvReader.read(&p, b"foo,RQly(%)\n0,100\n");
        assert!(matches!(r, Err(ReadError::Malformed { .. })));
    }

    #[test]
    fn profile_time_mapping_is_not_inert() {
        let incompatible = TMR_PROFILE.replace("tmr10ms", "other_tick");
        let profile = crate::profile::parse_profile(&incompatible, "public").expect("profile");
        let result = EdgeTxCsvReader.read(&profile, b"tmr10ms,RQly(%)\n3000,100\n");
        assert!(matches!(result, Err(ReadError::Profile(_))));
    }

    #[test]
    fn explicit_time_alternative_rejects_ambiguous_partial_and_missing_headers() {
        let composite = TMR_PROFILE.replace("time = \"tmr10ms\"", "time = \"Date,Time|tmr10ms\"");
        let profile = crate::profile::parse_profile(&composite, "public").expect("profile");
        for input in [
            b"Date,Time,tmr10ms,RQly(%)\n2026-08-31,09:00:00.000,3000,100\n".as_slice(),
            b"Date,RQly(%)\n2026-08-31,100\n".as_slice(),
            b"Time,RQly(%)\n09:00:00.000,100\n".as_slice(),
            b"other,RQly(%)\n0,100\n".as_slice(),
        ] {
            assert!(matches!(
                EdgeTxCsvReader.read(&profile, input),
                Err(ReadError::Malformed { .. })
            ));
        }
    }
}
