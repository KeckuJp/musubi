//! Explicit JSON decoding configuration; no directory discovery or profile overrides.
use crate::{ReadError, config::*};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
fn bad() -> ReadError {
    ReadError::Profile("invalid recorded reader profile".into())
}
fn string(m: &Map<String, Value>, key: &str) -> Result<String, ReadError> {
    let s = m.get(key).and_then(Value::as_str).ok_or_else(bad)?;
    if s.is_empty() || s.len() > 256 || s.chars().any(char::is_control) {
        return Err(bad());
    }
    Ok(s.into())
}
fn strings(m: &Map<String, Value>, key: &str) -> Result<Vec<String>, ReadError> {
    let Some(v) = m.get(key) else {
        return Ok(vec![]);
    };
    v.as_array()
        .ok_or_else(bad)?
        .iter()
        .map(|v| {
            v.as_str()
                .filter(|s| !s.is_empty() && s.len() <= 256)
                .map(str::to_owned)
                .ok_or_else(bad)
        })
        .collect()
}
pub fn parse_profile(text: &str) -> Result<ReadProfile, ReadError> {
    if text.len() > 256 * 1024 {
        return Err(bad());
    }
    let v: Value = musubi_jsonl_log::parse_unique_json(text.as_bytes()).map_err(|_| bad())?;
    let m = v.as_object().ok_or_else(bad)?;
    if m.keys().any(|k| {
        ![
            "profile_id",
            "version",
            "family",
            "source_role",
            "format",
            "extensions",
            "default_clock_basis",
            "channels",
            "fields",
            "units",
        ]
        .contains(&k.as_str())
    }) {
        return Err(bad());
    }
    let f = m.get("fields").and_then(Value::as_object).ok_or_else(bad)?;
    if f.keys()
        .any(|k| !["time", "status", "link", "battery", "gps", "health"].contains(&k.as_str()))
    {
        return Err(bad());
    }
    let mut units = BTreeMap::new();
    if let Some(v) = m.get("units") {
        for (key, value) in v.as_object().ok_or_else(bad)? {
            units.insert(key.clone(), value.as_str().ok_or_else(bad)?.into());
        }
    }
    let clock = ClockBasis::parse(&string(m, "default_clock_basis")?).ok_or_else(bad)?;
    if !matches!(
        clock,
        ClockBasis::Unknown | ClockBasis::BootRelative | ClockBasis::HostReceived
    ) {
        return Err(bad());
    }
    Ok(ReadProfile {
        profile_id: string(m, "profile_id")?,
        version: string(m, "version")?,
        family: Family::parse(&string(m, "family")?).ok_or_else(bad)?,
        source_role: SourceRole::parse(&string(m, "source_role")?).ok_or_else(bad)?,
        format: string(m, "format")?,
        extensions: strings(m, "extensions")?,
        channels: strings(m, "channels")?
            .iter()
            .map(|s| ChannelId::parse(s).ok_or_else(bad))
            .collect::<Result<_, _>>()?,
        default_clock_basis: clock,
        fields: FieldMapping {
            time: string(f, "time")?,
            status: strings(f, "status")?,
            link: strings(f, "link")?,
            battery: strings(f, "battery")?,
            gps: strings(f, "gps")?,
            health: strings(f, "health")?,
        },
        field_units: units,
    })
}
