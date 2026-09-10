//! Read decoded telemetry CSV without discarding unknown columns or their units.
//!
//! This is an unquoted, comma-separated format, not a raw Blackbox decoder or general CSV
//! implementation. Time is a strictly increasing, nonnegative boot-relative microsecond counter.
//! A bare time column means microseconds; an explicit unit must be `us`. No wall clock or platform
//! domain is inferred. Parsing is atomic: a malformed row returns an error, never partial output.

use std::collections::BTreeSet;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Integer(i64),
    Number(f64),
    Text(String),
    Blank,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Column {
    pub name: String,
    pub unit: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub source_line: usize,
    pub boot_us: u64,
    /// Same order and length as `Table::columns`, including unrecognized fields.
    pub values: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Table {
    pub columns: Vec<Column>,
    pub rows: Vec<Row>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    pub line: usize,
    pub reason: &'static str,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "line {}: {}", self.line, self.reason)
    }
}
impl std::error::Error for ParseError {}

fn error(line: usize, reason: &'static str) -> ParseError {
    ParseError { line, reason }
}

fn column(text: &str) -> Result<Column, ParseError> {
    let text = text.trim();
    let (name, unit) = match text.split_once(" (") {
        Some((name, suffix)) => {
            let unit = suffix
                .strip_suffix(')')
                .ok_or(error(1, "invalid unit suffix"))?;
            if unit.is_empty() || unit.contains(['(', ')']) {
                return Err(error(1, "invalid unit suffix"));
            }
            (name.trim(), Some(unit.to_owned()))
        }
        None => (text, None),
    };
    if name.is_empty() {
        return Err(error(1, "empty column name"));
    }
    Ok(Column {
        name: name.to_owned(),
        unit,
    })
}

fn value(text: &str, line: usize) -> Result<Value, ParseError> {
    let text = text.trim();
    if text.is_empty() {
        Ok(Value::Blank)
    } else if let Ok(integer) = text.parse::<i64>() {
        Ok(Value::Integer(integer))
    } else if let Ok(number) = text.parse::<f64>() {
        if !number.is_finite() {
            return Err(error(line, "non-finite number"));
        }
        // Preserve integers outside i64 instead of rounding them through f64.
        if text
            .trim_start_matches(['-', '+'])
            .bytes()
            .all(|b| b.is_ascii_digit())
        {
            Ok(Value::Text(text.to_owned()))
        } else {
            Ok(Value::Number(number))
        }
    } else {
        Ok(Value::Text(text.to_owned()))
    }
}

/// Parse a decoded telemetry table. `time_column` is a column name, without a unit suffix.
///
/// # Errors
/// Rejects raw/binary input, quotes, ragged rows, ambiguous columns, missing data, unsupported
/// time units and invalid or non-increasing timestamps.
pub fn parse(bytes: &[u8], time_column: &str) -> Result<Table, ParseError> {
    let text = std::str::from_utf8(bytes).map_err(|_| error(1, "not UTF-8"))?;
    if text.contains('"') || text.contains('\0') {
        return Err(error(1, "quoted or binary input is unsupported"));
    }
    let mut lines = text.lines();
    let header = lines.next().ok_or(error(1, "empty input"))?;
    let columns = header
        .split(',')
        .map(column)
        .collect::<Result<Vec<_>, _>>()?;
    let mut names = BTreeSet::new();
    if columns.iter().any(|c| !names.insert(c.name.as_str())) {
        return Err(error(1, "duplicate column name"));
    }
    let time_index = columns
        .iter()
        .position(|c| c.name == time_column)
        .ok_or(error(1, "missing time column"))?;
    if columns.len() < 2 {
        return Err(error(1, "no observation columns"));
    }
    if columns[time_index]
        .unit
        .as_deref()
        .is_some_and(|unit| unit != "us")
    {
        return Err(error(1, "time unit must be us"));
    }
    let mut rows = Vec::new();
    let mut previous = None;
    for (index, line) in lines.enumerate() {
        let line_number = index + 2;
        if line.trim().is_empty() {
            return Err(error(line_number, "blank data row"));
        }
        let cells = line.split(',').collect::<Vec<_>>();
        if cells.len() != columns.len() {
            return Err(error(line_number, "row width differs from header"));
        }
        let boot_us = cells[time_index]
            .trim()
            .parse::<i64>()
            .ok()
            .and_then(|v| u64::try_from(v).ok())
            .ok_or(error(line_number, "time must be a nonnegative integer"))?;
        if previous.is_some_and(|p| boot_us <= p) {
            return Err(error(line_number, "time must be strictly increasing"));
        }
        previous = Some(boot_us);
        let values = cells
            .iter()
            .map(|cell| value(cell, line_number))
            .collect::<Result<_, _>>()?;
        rows.push(Row {
            source_line: line_number,
            boot_us,
            values,
        });
    }
    if rows.is_empty() {
        return Err(error(2, "no data rows"));
    }
    Ok(Table { columns, rows })
}
