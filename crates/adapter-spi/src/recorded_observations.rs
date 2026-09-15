//! Meaning-labelled offline observations across saved formats. This API does not seal COM
//! envelopes or infer meanings, clocks, coordinate frames, identity, or device health.
use musubi_core::NormalizeError;
use musubi_decoded_csv::ParseOptions;
use musubi_recorded_readers::{ClockBasis, FieldValue, Observation, profile, reader_for};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const MAX_INPUT_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_OBSERVATIONS: usize = 100_000;
pub const MAX_REPORT_BYTES: usize = 128 * 1024 * 1024;

fn fail(source: &str, reason: &'static str) -> NormalizeError {
    crate::invalid(source, "recorded-observations/v1", reason, reason)
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn value(v: &FieldValue) -> Value {
    match v {
        FieldValue::I64(n) => json!(n),
        FieldValue::F64(n) => json!(n),
        FieldValue::Text(s) => json!(s),
        FieldValue::Blank => Value::Null,
    }
}
/// Explicit caller-reviewed meaning of one already converted field.
#[derive(Debug, Clone)]
pub struct FieldMeaning {
    pub meaning: String,
    pub unit: String,
    pub basis: String,
}
/// Original scalar value and its optional declared semantic interpretation.
#[derive(Debug, Clone)]
pub struct RecordedField {
    pub name: String,
    pub value: FieldValue,
    pub source_unit: Option<String>,
    pub meaning: Option<FieldMeaning>,
    pub disposition: &'static str,
}
/// Common offline observation. The decoded clock/channel and values remain inspectable.
/// This is not a sealed COM envelope and has no execution or transport capability.
#[derive(Debug, Clone)]
pub struct RecordedObservation {
    pub observation_index: usize,
    pub source: String,
    pub decoded: Observation,
    pub subject: Value,
    pub fields: Vec<RecordedField>,
}
impl RecordedObservation {
    #[must_use]
    pub fn to_json(&self) -> Value {
        let row = &self.decoded;
        let fields: Vec<_> = self
            .fields
            .iter()
            .map(|f| {
                json!({
                    "name":f.name,"value":value(&f.value),"source_unit":f.source_unit,
                    "meaning":f.meaning.as_ref().map(|m|&m.meaning),
                    "unit":f.meaning.as_ref().map(|m|&m.unit),
                    "basis":f.meaning.as_ref().map(|m|&m.basis),"disposition":f.disposition,
                })
            })
            .collect();
        json!({"observation_index":self.observation_index,"source":self.source,
            "source_role":row.source_role.as_str(),"channel":row.channel.as_str(),
            "t_ms":row.t_ms,"clock_basis":row.clock_basis.as_label(),
            "time_confidence":row.time_confidence,"t_boot_us":row.t_boot_us,
            "wall_ms":row.wall_ms,"anchor_unix_us":row.anchor_unix_us,
            "stale":row.stale,"subject":self.subject,"fields":fields})
    }
}
/// Source bytes are retained once, identified by hash. Meaning labels are declarations.
#[derive(Debug)]
pub struct IngestReport {
    document: Value,
    pub observations: Vec<RecordedObservation>,
}
impl IngestReport {
    #[must_use]
    pub fn to_json(&self) -> Value {
        let mut document = self.document.clone();
        document["observations"] = Value::Array(
            self.observations
                .iter()
                .map(RecordedObservation::to_json)
                .collect(),
        );
        document
    }
}

/// Decode and preserve input, then attach the caller's explicit interpretation to exact fields.
/// Unknown fields remain unnamed in the meaning vocabulary; no suffix-based unit inference.
///
/// # Errors
/// Rejects malformed declarations, source context, input bounds, unsupported clocks or formats,
/// and structural decoding failures atomically. The report is not a signed or sealed envelope.
pub fn ingest(
    bytes: &[u8],
    reader_profile_json: &str,
    meanings_json: &str,
    source_id: &str,
    received_at_ms: i64,
    options: ParseOptions,
) -> Result<IngestReport, NormalizeError> {
    if bytes.len() > MAX_INPUT_BYTES || meanings_json.len() > 256 * 1024 {
        return Err(fail(source_id, "input-oversize"));
    }
    musubi_core::quarantine::quarantine_str_field("identifier", source_id, 256)
        .map_err(|_| fail(source_id, "invalid-source"))?;
    if source_id.is_empty() || source_id.contains(';') || received_at_ms <= 0 {
        return Err(fail(source_id, "invalid-source-or-receipt"));
    }
    let profile = profile::parse_profile(reader_profile_json)
        .map_err(|_| fail(source_id, "invalid-profile"))?;
    let semantics: Value = musubi_jsonl_log::parse_unique_json(meanings_json.as_bytes())
        .map_err(|_| fail(source_id, "invalid-meanings"))?;
    let sm = semantics
        .as_object()
        .ok_or_else(|| fail(source_id, "invalid-meanings"))?;
    if semantics["schema"] != "recorded-meanings/v1"
        || sm
            .keys()
            .any(|k| !["schema", "subject", "fields"].contains(&k.as_str()))
    {
        return Err(fail(source_id, "invalid-meanings"));
    }
    let subject = semantics["subject"]
        .as_object()
        .ok_or_else(|| fail(source_id, "invalid-subject"))?;
    if subject.len() != 3
        || ["kind", "frame", "convention"].iter().any(|k| {
            !subject.get(*k).and_then(Value::as_str).is_some_and(|s| {
                !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control)
            })
        })
    {
        return Err(fail(source_id, "invalid-subject"));
    }
    let meanings = semantics["fields"]
        .as_object()
        .ok_or_else(|| fail(source_id, "invalid-meanings"))?;
    for (name, entry) in meanings {
        let m = entry
            .as_object()
            .ok_or_else(|| fail(source_id, "invalid-field-meaning"))?;
        if name.is_empty()
            || m.len() != 3
            || ["meaning", "unit", "basis"].iter().any(|k| {
                !m.get(*k).and_then(Value::as_str).is_some_and(|s| {
                    !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control)
                })
            })
            || !["reported", "declared", "calculated"]
                .contains(&entry["basis"].as_str().unwrap_or(""))
        {
            return Err(fail(source_id, "invalid-field-meaning"));
        }
    }
    let mut source_rows = None;
    let mut source_records = None;
    let mut accounting = json!({"source_record_count":"not-inferred-from-observation-count"});
    let mut decoded: Vec<Observation>;
    if profile.format == "telemetry_csv_us" {
        let report = musubi_recorded_readers::telemetry_csv::TelemetryCsvReader
            .read_report_with_options(&profile, bytes, options)
            .map_err(|_| fail(source_id, "invalid-input"))?;
        for column in &report.source_columns {
            if let (Some(header_unit), Some(meaning)) = (&column.unit, meanings.get(&column.name)) {
                if meaning["unit"].as_str() != Some(header_unit.as_str()) {
                    return Err(fail(source_id, "csv-meaning-unit-mismatch"));
                }
            }
        }
        source_rows = Some(report.source_rows);
        decoded = report.observations;
    } else {
        if options.allow_equal_time || options.preserve_nonfinite_as_text {
            return Err(fail(source_id, "csv-policy-for-non-csv-input"));
        }
        if profile.format == "miniseed2" {
            let report = musubi_recorded_readers::mseed_waveform::MseedWaveformReader
                .read_report(bytes)
                .map_err(|_| fail(source_id, "invalid-input"))?;
            accounting = json!({"records":report.records.len(),"samples_decoded":report.total_samples_decoded,
                "record_samples":report.accounting.iter().map(|r|json!({"record_index":r.record_index,"record_offset":r.record_offset,"record_length_bytes":r.record_length_bytes,"samples_declared":r.sample_count_declared,"samples_decoded":r.sample_count_decoded,"unknown_blockette_bytes":r.unknown_blockette_bytes})).collect::<Vec<_>>()});
            decoded = report.observations;
        } else if profile.format == "ardupilot_dataflash_bin" {
            let report = musubi_recorded_readers::bin::ArduPilotBinReader
                .read_report(&profile, bytes)
                .map_err(|_| fail(source_id, "invalid-input"))?;
            accounting = json!({"data_records":report.data_records,
                "untimed_records":report.untimed_records.iter().map(|r|json!({
                    "offset":r.offset,"byte_length":r.bytes.len(),
                    "fields":r.fields.iter().map(|(name,v)|json!({"name":name,"value":value(v)})).collect::<Vec<_>>()
                })).collect::<Vec<_>>()});
            decoded = report.observations;
        } else if profile.format == "px4_ulog" {
            let report = musubi_recorded_readers::ulog::Px4UlgReader
                .read_report(&profile, bytes)
                .map_err(|_| fail(source_id, "invalid-input"))?;
            accounting = json!({"selected_multi_id":report.selected_multi_id,
                "data_records":report.data_records,"decoded_data_records":report.decoded_data_records,
                "unsupported_records":report.unsupported_records.iter().map(|r|json!({
                    "offset":r.offset,"byte_length":r.bytes.len(),"reason":r.reason
                })).collect::<Vec<_>>()});
            decoded = report.observations;
        } else if profile.format == "pymavlink_dataflash_jsonl" {
            let report = musubi_recorded_readers::mavlog_json::MavlogJsonReader
                .read_report(&profile, bytes)
                .map_err(|_| fail(source_id, "invalid-input"))?;
            accounting = json!({"untimed_records":report.untimed_records,"retained_records":report.records.len()});
            source_records = Some(report.records);
            decoded = report.observations;
        } else {
            decoded = reader_for(&profile.format)
                .ok_or_else(|| fail(source_id, "unsupported-format"))?
                .read(&profile, bytes)
                .map_err(|_| fail(source_id, "invalid-input"))?;
        }
    }
    if decoded.len() > MAX_OBSERVATIONS {
        return Err(fail(source_id, "observation-count-bound"));
    }
    if profile.default_clock_basis == ClockBasis::Unknown {
        for row in &mut decoded {
            row.clock_basis = ClockBasis::Unknown;
            row.time_confidence = 0.0;
            row.t_boot_us = None;
            row.wall_ms = None;
            row.anchor_unix_us = None;
        }
    }
    let observations: Vec<_> = decoded
        .into_iter()
        .enumerate()
        .map(|(index, row)| {
            let fields = row
                .fields
                .iter()
                .map(|(name, v)| {
                    let meaning = meanings.get(name).map(|m| FieldMeaning {
                        meaning: m["meaning"].as_str().unwrap_or_default().to_owned(),
                        unit: m["unit"].as_str().unwrap_or_default().to_owned(),
                        basis: m["basis"].as_str().unwrap_or_default().to_owned(),
                    });
                    let disposition = match v {
                        FieldValue::Blank => "blank",
                        FieldValue::Text(s)
                            if s.starts_with("nonfinite_")
                                || s.parse::<f64>().is_ok_and(|v| !v.is_finite()) =>
                        {
                            "nonfinite-as-text"
                        }
                        _ => "present",
                    };
                    RecordedField {
                        name: name.clone(),
                        value: v.clone(),
                        source_unit: profile.field_units.get(name).cloned(),
                        meaning,
                        disposition,
                    }
                })
                .collect();
            RecordedObservation {
                observation_index: index,
                source: source_id.to_owned(),
                decoded: row,
                subject: Value::Object(subject.clone()),
                fields,
            }
        })
        .collect();
    let source_hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    let mut document = json!({"schema":"recorded-observation-report/v1","com_sealed":false,
        "source_id":source_id,"received_at_ms":received_at_ms,"input_sha256":hash(bytes),
        "profile_sha256":hash(reader_profile_json.as_bytes()),"meanings_sha256":hash(meanings_json.as_bytes()),
        "source_bytes":bytes.len(),"source_hex":source_hex,"source_record_count":source_rows,
        "accounting":accounting,"observation_count":observations.len()});
    if let Some(records) = source_records {
        document["value_basis"] = json!("pymavlink_decoded_no_second_wire_scaling");
        document["source_records"] = json!(records);
    }
    Ok(IngestReport {
        document,
        observations,
    })
}
