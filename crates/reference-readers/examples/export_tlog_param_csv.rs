//! Passive saved configuration-read records -> existing declared response comparison.
use musubi_reference_readers::{
    ClockBasis, FieldValue, Observation, ProfileReader, SourceRole, field, profile::parse_profile,
    tlog::TlogReader,
};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
type Error = Box<dyn std::error::Error>;
const LIMIT: usize = 16 * 1024 * 1024;
const HEADER: &str = "record_time_us,msg,param_id,param_index,target_sys,target_comp,src_sys,src_comp,session,expected_param_type,param_type,param_count,param_value_raw_hex,source_record_index,source_input_sha256,source_profile_sha256,source_observation_digest,source_raw_frame_hex,source_total_records,source_request_records,source_response_records,source_other_records,basis\n";
const BASIS: &str = "saved_reported_read_only_configuration_request_and_broadcast_value_not_causal_reply;session_and_expected_type_caller_declared;host_clock_unknown;value_bytes_encoding_unqualified_not_numeric;retain_derived_csv_with_native_input";

fn bounded(path: &str, limit: usize) -> Result<Vec<u8>, Error> {
    if !std::fs::metadata(path)?.is_file() {
        return Err("saved regular file required".into());
    }
    let mut raw = Vec::new();
    std::fs::File::open(path)?
        .take((limit + 1) as u64)
        .read_to_end(&mut raw)?;
    if raw.is_empty() || raw.len() > limit {
        return Err("empty or oversized saved file".into());
    }
    Ok(raw)
}
fn number(row: &Observation, key: &str) -> Result<i64, Error> {
    match field(row, key) {
        Some(FieldValue::I64(v)) => Ok(*v),
        _ => Err(format!("missing {key}").into()),
    }
}
fn text<'a>(row: &'a Observation, key: &str) -> Result<&'a str, Error> {
    match field(row, key) {
        Some(FieldValue::Text(v)) => Ok(v),
        _ => Err(format!("missing {key}").into()),
    }
}
fn token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|v| v.is_ascii_alphanumeric() || b"._-".contains(&v))
}
fn main() -> Result<(), Error> {
    let a: Vec<_> = std::env::args().collect();
    if a.len() != 9 || a[3] != "--requester" || a[5] != "--session" || a[7] != "--expected-type" {
        return Err("usage: export_tlog_param_csv PROFILE INPUT --requester SYS:COMP --session TOKEN --expected-type UINT8".into());
    }
    let peer: Vec<_> = a[4].split(':').collect();
    if peer.len() != 2 || !token(&a[6]) || !a[8].bytes().all(|v| v.is_ascii_digit()) {
        return Err("invalid explicit requester/session/type".into());
    }
    let requester = (peer[0].parse::<u8>()?, peer[1].parse::<u8>()?);
    if requester.0 == 0 || requester.1 == 0 {
        return Err("nonzero requester required".into());
    }
    let expected = a[8].parse::<u8>()?.to_string();
    let profile_raw = bounded(&a[1], 64 * 1024)?;
    let profile = parse_profile(std::str::from_utf8(&profile_raw)?, "development")
        .map_err(|e| format!("profile: {e:?}"))?;
    if profile.format != "mavlink_tlog"
        || profile.source_role != SourceRole::Gcs
        || profile.default_clock_basis != ClockBasis::Unknown
        || profile.fields.time != "host_recorded_us"
        || profile.declared_sender.is_some()
    {
        return Err(
            "unfiltered GCS recorded-host Unknown profile required for both parties".into(),
        );
    }
    let raw = bounded(&a[2], LIMIT)?;
    let rows = TlogReader
        .read(&profile, &raw)
        .map_err(|e| format!("input: {e:?}"))?;
    let mut kinds = Vec::with_capacity(rows.len());
    let mut requests = 0usize;
    let mut responses = 0usize;
    for row in &rows {
        let id = number(row, "MAVLINK.message_id")?;
        let peer = (
            number(row, "MAVLINK.system_id")?,
            number(row, "MAVLINK.component_id")?,
        );
        let kind = if id == 20 && peer == (i64::from(requester.0), i64::from(requester.1)) {
            requests += 1;
            "PARAM_REQUEST_READ"
        } else if id == 22 {
            responses += 1;
            "PARAM_VALUE"
        } else {
            "OTHER"
        };
        kinds.push(kind);
    }
    if requests == 0 {
        return Err("no selected saved requests".into());
    }
    let source_hash = format!("sha256:{:x}", Sha256::digest(&raw));
    let profile_hash = format!("sha256:{:x}", Sha256::digest(&profile_raw));
    let mut output = String::from(HEADER);
    for (index, (row, kind)) in rows.iter().zip(kinds).enumerate() {
        let mut values = vec![String::new(); 23];
        values[0] = text(row, "MAVLINK.host_recorded_us")?.to_owned();
        if values[0].is_empty() || !values[0].bytes().all(|v| v.is_ascii_digit()) {
            return Err("invalid original recorded time".into());
        }
        values[1] = kind.into();
        if kind != "OTHER" {
            let id = text(row, &format!("{kind}.param_id_hex"))?;
            values[2] = if id.is_empty() {
                String::new()
            } else {
                format!("hex:{id}")
            };
            values[3] = number(row, &format!("{kind}.param_index"))?.to_string();
            if kind == "PARAM_REQUEST_READ" {
                for (column, field) in [(4, "target_system"), (5, "target_component")] {
                    let value = number(row, &format!("{kind}.{field}"))?;
                    if value != 0 {
                        values[column] = value.to_string();
                    }
                }
                values[9] = expected.clone();
            } else {
                values[10] = number(row, "PARAM_VALUE.param_type")?.to_string();
                values[11] = number(row, "PARAM_VALUE.param_count")?.to_string();
                values[12] = format!("hex:{}", text(row, "PARAM_VALUE.value_raw_hex")?);
            }
        }
        for (column, field) in [(6, "MAVLINK.system_id"), (7, "MAVLINK.component_id")] {
            let value = number(row, field)?;
            if value != 0 {
                values[column] = value.to_string();
            }
        }
        values[8] = a[6].clone();
        values[13] = index.to_string();
        values[14] = source_hash.clone();
        values[15] = profile_hash.clone();
        values[16] = format!(
            "sha256:{}",
            row.digest
                .0
                .iter()
                .map(|v| format!("{v:02x}"))
                .collect::<String>()
        );
        values[17] = text(row, "MAVLINK.raw_frame_hex")?.into();
        values[18] = rows.len().to_string();
        values[19] = requests.to_string();
        values[20] = responses.to_string();
        values[21] = (rows.len() - requests - responses).to_string();
        values[22] = BASIS.into();
        output.push_str(&values.join(","));
        output.push('\n');
        if output.len() > LIMIT {
            return Err("derived CSV byte limit exceeded".into());
        }
    }
    // Validate the complete saved source/export before emitting any stdout.
    std::io::stdout().lock().write_all(output.as_bytes())?;
    Ok(())
}
