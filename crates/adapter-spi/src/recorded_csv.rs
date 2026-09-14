//! Direct, bounded boot-counter CSV to the existing geodetic common-model mapper.
//! The original source is retained once, outside the sealed observation content.
use crate::recorded_jsonl::{
    MAX_INPUT_BYTES, MAX_RECORD_BYTES, MappedRecord, Mapping, RecordedJsonlNormalizer,
    RejectedRecord, diagnostic_records,
};
use musubi_core::NormalizeError;
use musubi_decoded_csv::{ParseOptions, Table, Value as Cell, parse_with_options};
use serde_json::{Map, Value, json};

pub const MAX_RECORDS: usize = 100_000;
pub const MAX_REPORT_BYTES: usize = 128 * 1024 * 1024;

fn failure(source: &str, reason: &'static str) -> NormalizeError {
    crate::invalid(source, "recorded-csv/v1", reason, reason)
}

#[derive(Debug)]
pub struct IngestReport {
    pub table: Table,
    pub source_csv: String,
    pub mapped: Vec<MappedRecord>,
    pub rejected: Vec<RejectedRecord>,
}

/// The caller declares boot-relative microseconds and the mapping's deg/m-MSL semantics.
/// Parse once and account for every row as a mapped or indexed INVALID observation.
/// Structural errors reject atomically. This is not a decoder or clock/datum inference.
///
/// # Errors
/// Rejects input/line/row bounds, invalid CSV or time, mismatched declared column units,
/// invalid source identifiers and nonpositive receipt times.
pub fn ingest_boot_csv(
    bytes: &[u8],
    time_column: &str,
    mapping: &Mapping,
    source_id: &str,
    received_at: i64,
    options: ParseOptions,
) -> Result<IngestReport, NormalizeError> {
    if bytes.len() > MAX_INPUT_BYTES {
        return Err(failure(source_id, "input-oversize"));
    }
    let text = std::str::from_utf8(bytes).map_err(|_| failure(source_id, "invalid-csv"))?;
    if text.lines().any(|line| line.len() > MAX_RECORD_BYTES)
        || text.lines().count() > MAX_RECORDS + 1
    {
        return Err(failure(source_id, "csv-line-or-row-bound"));
    }
    // Validate the caller context even if every measurement will be rejected.
    musubi_core::quarantine::quarantine_str_field("identifier", source_id, 256)
        .map_err(|_| failure(source_id, "invalid-source"))?;
    if source_id.is_empty() || source_id.contains(';') || received_at <= 0 {
        return Err(failure(source_id, "invalid-source-or-received-at"));
    }
    let table = parse_with_options(bytes, time_column, options)
        .map_err(|_| failure(source_id, "invalid-csv"))?;
    // CSV has flat source columns. Generated metadata is a declaration, not a source label.
    if let Some(path) = &mapping.source_channel_path {
        if path.len() != 2 || path[0] != "data" {
            return Err(failure(source_id, "invalid-csv-channel-path"));
        }
    }
    for (field, unit) in [
        (&mapping.latitude_field, "deg"),
        (&mapping.longitude_field, "deg"),
        (&mapping.altitude_field, "m"),
    ] {
        let column = table
            .columns
            .iter()
            .find(|column| &column.name == field)
            .ok_or_else(|| failure(source_id, "missing-position-column"))?;
        if column
            .unit
            .as_deref()
            .is_some_and(|declared| declared != unit)
        {
            return Err(failure(source_id, "position-unit-mismatch"));
        }
    }
    let normalizer = RecordedJsonlNormalizer::new(mapping.clone());
    let mut mapped = Vec::new();
    let mut rejected = Vec::new();
    for (record_index, row) in table.rows.iter().enumerate() {
        let data: Map<String, Value> = table
            .columns
            .iter()
            .zip(&row.values)
            .map(|(column, value)| {
                let value = match value {
                    Cell::Integer(value) => json!(value),
                    Cell::Number(value) => json!(value),
                    Cell::Text(value) => json!(value),
                    Cell::Blank => Value::Null,
                };
                (column.name.clone(), value)
            })
            .collect();
        let record = json!({"meta":{"type":mapping.message_type},"data":data});
        match normalizer.normalize_record(&record, source_id, received_at) {
            Ok(envelope) => mapped.push(MappedRecord {
                record_index,
                boot_us: row.boot_us,
                envelope,
            }),
            Err(error) => rejected.push(RejectedRecord {
                record_index,
                error,
            }),
        }
    }
    Ok(IngestReport {
        table,
        source_csv: text.to_owned(),
        mapped,
        rejected,
    })
}

impl IngestReport {
    /// Diagnostic JSON, not a canonical envelope export. Original CSV appears exactly once.
    pub fn to_json(&self) -> Value {
        let (mapped, rejected) = diagnostic_records(&self.mapped, &self.rejected);
        json!({"schema":"recorded-csv-report/v1", "source_records_sealed":false,
            "source_csv":self.source_csv, "source_record_count":self.table.rows.len(),
            "clock_basis":"boot-microseconds-declared", "message_type_basis":"profile-declared",
            "mapped":mapped, "rejected":rejected})
    }
}
