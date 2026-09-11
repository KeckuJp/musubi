//! Bounded, offline mapping of decoded geodetic records. Retained source values are an
//! unsealed sidecar: the envelope digest covers mapped COM and claim content only.
use crate::spi::{invalid, quarantine_error};
use musubi_core::{
    NormalizeError, Normalizer, ObservationQuality, QualityNote, RawObservation, derive_mark,
    quarantine, seal_digest,
};
use musubi_jsonl_log::{ReadReport, Value};
use musubi_types::{
    Claim, ComObject, ConfidenceBasis, EvidenceEnvelope, PlatformDomain, PlatformState, Position,
    Timestamps,
};
use serde_json::json;

pub const ADAPTER_SLUG: &str = "recorded-jsonl/v1";
pub const MAX_INPUT_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_RECORD_BYTES: usize = 64 * 1024;
pub const MAX_PROFILE_BYTES: usize = 4096;
const MAX_DEPTH: usize = 32;

fn failure(source: &str, reason: &'static str) -> NormalizeError {
    invalid(source, ADAPTER_SLUG, reason, reason)
}
fn bounded(bytes: &[u8], max: usize, source: &str) -> Result<(), NormalizeError> {
    quarantine::quarantine_payload_size(bytes, max)
        .and_then(|()| quarantine::reject_if_json_nesting_exceeds(bytes, MAX_DEPTH))
        .map_err(|e| quarantine_error(source, ADAPTER_SLUG, e))
}
fn token(value: &str, source: &str) -> Result<(), NormalizeError> {
    quarantine::quarantine_str_field("identifier", value, 256)
        .map_err(|e| quarantine_error(source, ADAPTER_SLUG, e))?;
    if value.is_empty() || value.contains(';') {
        return Err(failure(source, "invalid-identifier"));
    }
    Ok(())
}

/// Human-declared semantics, never inferred from a filename or local pose values.
#[derive(Debug, Clone)]
pub struct Mapping {
    message_type: String,
    latitude_field: String,
    longitude_field: String,
    altitude_field: String,
    domain: PlatformDomain,
    fix: Option<(String, u64)>,
}
impl Mapping {
    /// Parse a single JSON object. Unknown keys and unsupported units are errors.
    /// Duplicate keys are rejected using the existing reader's unique-key parser.
    pub fn parse(bytes: &[u8]) -> Result<Self, NormalizeError> {
        let source = "mapping-profile";
        bounded(bytes, MAX_PROFILE_BYTES, source)?;
        let mut wrapped =
            b"{\"meta\":{\"type\":\"profile\"},\"data\":{\"TimeUS\":0},\"profile\":".to_vec();
        wrapped.extend_from_slice(bytes);
        wrapped.push(b'}');
        // JSONL's line contract needs a compact profile, but editable profiles may be multiline.
        // Replace line breaks outside strings with spaces, preserving token boundaries.
        let mut quoted = false;
        let mut escaped = false;
        for b in &mut wrapped {
            if quoted {
                if escaped {
                    escaped = false;
                } else if *b == b'\\' {
                    escaped = true;
                } else if *b == b'"' {
                    quoted = false;
                }
            } else if *b == b'"' {
                quoted = true;
            } else if *b == b'\n' || *b == b'\r' {
                *b = b' ';
            }
        }
        let report =
            musubi_jsonl_log::parse(&wrapped).map_err(|_| failure(source, "invalid-profile"))?;
        if report.records.len() != 1 || report.records[0].as_object().map(|o| o.len()) != Some(3) {
            return Err(failure(source, "invalid-profile"));
        }
        let obj = report.records[0]["profile"]
            .as_object()
            .ok_or_else(|| failure(source, "invalid-profile"))?;
        let keys = [
            "message_type",
            "latitude_field",
            "longitude_field",
            "altitude_field",
            "domain",
            "fix_field",
            "min_fix",
            "position_units",
        ];
        if obj.keys().any(|k| !keys.contains(&k.as_str())) {
            return Err(failure(source, "unknown-profile-key"));
        }
        let field = |name: &str| -> Result<String, NormalizeError> {
            let value = obj
                .get(name)
                .and_then(Value::as_str)
                .ok_or_else(|| failure(source, "invalid-profile"))?;
            token(value, source)?;
            Ok(value.to_owned())
        };
        if field("position_units")? != "deg-m-msl" {
            return Err(failure(source, "unsupported-units"));
        }
        let domain = match obj.get("domain").and_then(Value::as_str) {
            None if !obj.contains_key("domain") => PlatformDomain::Unknown,
            Some("unknown") => PlatformDomain::Unknown,
            Some("air") => PlatformDomain::Air,
            Some("surface") => PlatformDomain::Surface,
            Some("ground") => PlatformDomain::Ground,
            _ => return Err(failure(source, "invalid-domain")),
        };
        let fix = match (obj.get("fix_field"), obj.get("min_fix")) {
            (None, None) => None,
            (Some(_), Some(min)) => Some((
                field("fix_field")?,
                min.as_u64()
                    .ok_or_else(|| failure(source, "invalid-fix-policy"))?,
            )),
            _ => return Err(failure(source, "invalid-fix-policy")),
        };
        Ok(Self {
            message_type: field("message_type")?,
            latitude_field: field("latitude_field")?,
            longitude_field: field("longitude_field")?,
            altitude_field: field("altitude_field")?,
            domain,
            fix,
        })
    }
}

#[derive(Debug, Clone)]
pub struct RecordedJsonlNormalizer {
    mapping: Mapping,
}
impl RecordedJsonlNormalizer {
    pub fn new(mapping: Mapping) -> Self {
        Self { mapping }
    }
    pub fn manifest(&self) -> crate::SourceManifest {
        crate::SourceManifest::read_only()
    }
}
impl Normalizer for RecordedJsonlNormalizer {
    fn normalize(&self, raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError> {
        let source = &raw.source_id;
        bounded(&raw.payload, MAX_RECORD_BYTES, source)?;
        token(source, "invalid-source")?;
        if raw.received_at <= 0 {
            return Err(failure(source, "invalid-received-at"));
        }
        let report =
            musubi_jsonl_log::parse(&raw.payload).map_err(|_| failure(source, "invalid-record"))?;
        if report.records.len() != 1 {
            return Err(failure(source, "expected-one-record"));
        }
        let record = &report.records[0];
        let m = &self.mapping;
        if record["meta"]["type"].as_str() != Some(&m.message_type) {
            return Err(failure(source, "unselected-record"));
        }
        let data = &record["data"];
        let number = |field: &str| {
            data.get(field)
                .and_then(Value::as_f64)
                .filter(|v| v.is_finite())
                .ok_or_else(|| failure(source, "invalid-position-field"))
        };
        let lat = number(&m.latitude_field)?;
        let lon = number(&m.longitude_field)?;
        let alt = number(&m.altitude_field)?;
        if !(-90.0..=90.0).contains(&lat) || !(-180.0..=180.0).contains(&lon) {
            return Err(failure(source, "position-out-of-range"));
        }
        if let Some((field, min)) = &m.fix {
            let fix = data
                .get(field)
                .and_then(Value::as_u64)
                .ok_or_else(|| failure(source, "invalid-fix-field"))?;
            if fix < *min {
                return Err(failure(source, "insufficient-fix"));
            }
        }
        let notes = vec![
            QualityNote::neutral("position-units:deg-m-msl-declared"),
            QualityNote::neutral("timestamp:boot-counter-not-epoch"),
            QualityNote::neutral("confidence:adapter-assigned"),
            QualityNote::neutral("source-sidecar:not-sealed"),
        ];
        let mark = derive_mark(&ObservationQuality {
            source_id: source.clone(),
            adapter_slug: ADAPTER_SLUG.into(),
            time_confidence: 0.0,
            notes,
        });
        Ok(seal_digest(EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: source.clone(),
                position: Some(Position {
                    lat_deg: lat,
                    lon_deg: lon,
                    alt_m: Some(alt),
                }),
                mode: None,
                timestamps: Timestamps {
                    observed_at: None,
                    received_at: raw.received_at,
                    time_confidence: 0.0,
                },
                platform_domain: m.domain,
            }),
            claim: Claim {
                claim_id: format!("{source}:recorded-jsonl"),
                confidence: 0.0,
                confidence_basis: Some(ConfidenceBasis::AdapterAssigned),
                mark,
            },
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }))
    }
}

#[derive(Debug)]
pub struct MappedRecord {
    pub record_index: usize,
    pub boot_us: u64,
    pub envelope: EvidenceEnvelope,
}
#[derive(Debug)]
pub struct RejectedRecord {
    pub record_index: usize,
    pub error: NormalizeError,
}
#[derive(Debug)]
pub struct IngestReport {
    pub reader: ReadReport,
    pub mapped: Vec<MappedRecord>,
    pub rejected: Vec<RejectedRecord>,
    pub untimed_indices: Vec<usize>,
    pub unmapped_indices: Vec<usize>,
}
/// Parse atomically, then account for every record in exactly one category.
/// Selected malformed positions return indexed INVALID outcomes, never disappear.
pub fn ingest(
    bytes: &[u8],
    mapping: &Mapping,
    source_id: &str,
    received_at: i64,
) -> Result<IngestReport, NormalizeError> {
    bounded(bytes, MAX_INPUT_BYTES, source_id)?;
    token(source_id, "invalid-source")?;
    if received_at <= 0 {
        return Err(failure(source_id, "invalid-received-at"));
    }
    for line in bytes.split(|b| *b == b'\n') {
        bounded(line, MAX_RECORD_BYTES, source_id)?;
    }
    let reader = musubi_jsonl_log::parse(bytes).map_err(|_| failure(source_id, "invalid-jsonl"))?;
    let mut report = IngestReport {
        reader,
        mapped: vec![],
        rejected: vec![],
        untimed_indices: vec![],
        unmapped_indices: vec![],
    };
    let normalizer = RecordedJsonlNormalizer::new(mapping.clone());
    let mut times = report.reader.timed_records.iter().peekable();
    for (index, record) in report.reader.records.iter().enumerate() {
        let Some(timed) = times.peek().filter(|t| t.record_index == index) else {
            report.untimed_indices.push(index);
            continue;
        };
        let boot_us = timed.boot_us;
        times.next();
        if record["meta"]["type"].as_str() != Some(&mapping.message_type) {
            report.unmapped_indices.push(index);
            continue;
        }
        let raw = RawObservation {
            source_id: source_id.into(),
            payload: record.to_string().into_bytes(),
            received_at,
        };
        match normalizer.normalize(&raw) {
            Ok(envelope) => report.mapped.push(MappedRecord {
                record_index: index,
                boot_us,
                envelope,
            }),
            Err(error) => report.rejected.push(RejectedRecord {
                record_index: index,
                error,
            }),
        }
    }
    if report.mapped.is_empty() && report.rejected.is_empty() {
        return Err(failure(source_id, "no-selected-timed-records"));
    }
    Ok(report)
}
impl IngestReport {
    /// Diagnostic report, not a canonical envelope serializer or signed export.
    pub fn to_json(&self) -> Value {
        let mapped: Vec<Value> = self.mapped.iter().map(|r| {
            let ComObject::PlatformState(p) = &r.envelope.observation else { unreachable!() };
            let pos = p.position.as_ref().expect("normalizer always supplies position");
            let digest: String = r.envelope.content_digest.unwrap().iter().map(|b| format!("{b:02x}")).collect();
            json!({"record_index":r.record_index,"boot_us":r.boot_us,
                "platform_id":p.platform_id,"domain":format!("{:?}",p.platform_domain),
                "position":{"lat_deg":pos.lat_deg,"lon_deg":pos.lon_deg,"alt_m":pos.alt_m},
                "observed_at":p.timestamps.observed_at,"received_at":p.timestamps.received_at,
                "time_confidence":p.timestamps.time_confidence,"confidence":r.envelope.claim.confidence,
                "confidence_basis":"AdapterAssigned","mark":format!("{:?}",r.envelope.claim.mark.status),
                "reason_code":r.envelope.claim.mark.reason_code,"provenance":r.envelope.claim.mark.provenance,
                "content_digest":digest})
        }).collect();
        let rejected: Vec<Value> = self
            .rejected
            .iter()
            .map(|r| {
                json!({"record_index":r.record_index,
            "source_id":r.error.source_id,"mark":format!("{:?}",r.error.mark.status),
            "reason_code":r.error.mark.reason_code,"provenance":r.error.mark.provenance})
            })
            .collect();
        json!({"schema":"recorded-jsonl-report/v1","source_records_sealed":false,
            "records":self.reader.records,"mapped":mapped,"rejected":rejected,
            "untimed_indices":self.untimed_indices,"unmapped_indices":self.unmapped_indices})
    }
}
