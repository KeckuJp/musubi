use musubi_reference_types::{ClockBasis, Family, FamilyProfile};
use musubi_types::PlatformDomain;
use serde_json::Value;

use crate::{FieldValue, Observation, ProfileReader, ReadError, observation};

#[derive(Debug)]
pub struct ReadReport {
    pub observations: Vec<Observation>,
    pub records: Vec<Value>,
    pub untimed_records: usize,
    pub platform_domain: PlatformDomain,
    pub firmware_identity: Option<String>,
}

#[derive(Debug, Clone, Copy)]
pub struct MavlogJsonReader;

fn malformed(line: usize, what: &str) -> ReadError {
    ReadError::Malformed {
        offset: line,
        what: what.into(),
    }
}

fn field_value(value: &Value) -> FieldValue {
    match value {
        Value::Number(n) if n.as_i64().is_some() => FieldValue::I64(n.as_i64().unwrap_or_default()),
        Value::Number(n) if !n.is_u64() => FieldValue::F64(n.as_f64().unwrap_or_default()),
        Value::String(s) => FieldValue::Text(s.clone()),
        Value::Null => FieldValue::Blank,
        other => FieldValue::Text(other.to_string()),
    }
}

impl MavlogJsonReader {
    pub fn read_report(
        &self,
        profile: &FamilyProfile,
        bytes: &[u8],
    ) -> Result<ReadReport, ReadError> {
        if profile.format != self.format_id() {
            return Err(ReadError::Profile(
                "expected pymavlink_dataflash_jsonl profile".into(),
            ));
        }
        let decoded = musubi_jsonl_log::parse(bytes).map_err(|e| malformed(e.line, e.reason))?;
        let times: std::collections::BTreeMap<_, _> = decoded
            .timed_records
            .iter()
            .map(|t| (t.record_index, t.boot_us))
            .collect();
        let mut report = ReadReport {
            observations: vec![],
            records: vec![],
            untimed_records: 0,
            platform_domain: PlatformDomain::Unknown,
            firmware_identity: None,
        };
        let mut identity_family = None;
        let mut rover_frame = None;
        for (index, record) in decoded.records.into_iter().enumerate() {
            let line_no = index + 1;
            let meta = record
                .get("meta")
                .and_then(Value::as_object)
                .ok_or_else(|| malformed(line_no, "missing meta object"))?;
            let name = meta
                .get("type")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .ok_or_else(|| malformed(line_no, "missing message type"))?;
            let data = record
                .get("data")
                .and_then(Value::as_object)
                .ok_or_else(|| malformed(line_no, "missing data object"))?;
            if name == "MSG" {
                if let Some(message) = data.get("Message").and_then(Value::as_str) {
                    let family = if message.starts_with("ArduPlane ") {
                        Some(Family::FixedWing)
                    } else if message.starts_with("ArduRover ") || message.starts_with("Rover ") {
                        Some(Family::Ugv)
                    } else {
                        None
                    };
                    if let Some(family) = family {
                        if identity_family.is_some_and(|old| old != family)
                            || family != profile.family
                        {
                            return Err(malformed(
                                line_no,
                                "firmware identity contradicts profile or previous identity",
                            ));
                        }
                        identity_family = Some(family);
                        report.firmware_identity = Some(message.into());
                    }
                }
            }
            if profile.family == Family::Ugv
                && name == "PARM"
                && data.get("Name").and_then(Value::as_str) == Some("FRAME_CLASS")
            {
                let frame = data
                    .get("Value")
                    .and_then(Value::as_f64)
                    .ok_or_else(|| malformed(line_no, "invalid FRAME_CLASS"))?;
                if rover_frame.is_some_and(|old| old != frame) {
                    return Err(malformed(line_no, "conflicting FRAME_CLASS"));
                }
                rover_frame = Some(frame);
            }
            let sensor_local_ms = !data.contains_key("TimeUS")
                && (name.starts_with("ACC") || name.starts_with("GYR"));
            if let Some(&t_us) = times.get(&index).filter(|_| !sensor_local_ms) {
                let t_ms =
                    i64::try_from(t_us / 1000).map_err(|_| malformed(line_no, "time overflow"))?;
                let fields = data
                    .iter()
                    .map(|(key, val)| (format!("{name}.{key}"), field_value(val)))
                    .collect();
                let mut o =
                    observation(profile, t_ms, crate::bin::channel_for(name), fields, false);
                o.t_boot_us = Some(t_us);
                o.clock_basis = ClockBasis::BootRelative;
                o.wall_ms = None;
                o.anchor_unix_us = None;
                o.time_confidence = crate::default_time_confidence(ClockBasis::BootRelative);
                report.observations.push(o);
            } else {
                report.untimed_records += 1;
            }
            report.records.push(record);
        }
        if report.records.is_empty() || report.observations.is_empty() {
            return Err(malformed(0, "no timed DataFlash observations"));
        }
        report.platform_domain = match (identity_family, rover_frame) {
            (Some(Family::FixedWing), _) => PlatformDomain::Air,
            (Some(Family::Ugv), Some(1.0)) => PlatformDomain::Ground,
            (Some(Family::Ugv), Some(2.0)) => PlatformDomain::Surface,
            _ => PlatformDomain::Unknown,
        };
        Ok(report)
    }
}

impl ProfileReader for MavlogJsonReader {
    fn format_id(&self) -> &'static str {
        "pymavlink_dataflash_jsonl"
    }
    fn read(&self, profile: &FamilyProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        self.read_report(profile, bytes)
            .map(|report| report.observations)
    }
}
