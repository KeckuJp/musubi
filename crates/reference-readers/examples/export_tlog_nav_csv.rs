//! Saved TLOG common Observations -> input for the existing declared-pair check.
//! No decoder, comparator, clock promotion or physical-route assertion lives here.
use musubi_reference_readers::{
    ClockBasis, FieldValue, Observation, ProfileReader, SourceRole, field, profile::parse_profile,
    tlog::TlogReader,
};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};

const LIMIT: usize = 16 * 1024 * 1024;
const HEADER: &str = "record_time_us,reported_xtrack_m,declared_reference_m,source_input_sha256,source_profile_sha256,source_selected_index,source_observation_digest,reported_system_id,reported_component_id,reported_message_id,source_raw_frame_hex,source_selected_records,source_unselected_records,source_value_basis,condition_basis\n";
const VALUE_BASIS: &str = "MAVLink_common_3203f89c510337c0088244735c6a5056c52b5a28_NAV_CONTROLLER_OUTPUT_xy_crosstrack_m_sign_preserved_positive_side_and_route_identity_unspecified_not_physical_truth";
const CONDITION_BASIS: &str = "caller_declared_reference_m_and_separately_declared_tolerance_m_not_authenticated_route_or_recommended_threshold";
type Error = Box<dyn std::error::Error>;

fn bounded_file(path: &str, limit: usize) -> Result<Vec<u8>, Error> {
    if !std::fs::metadata(path)?.is_file() {
        return Err("input must be a saved regular file".into());
    }
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take((limit + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err("saved input byte bound exceeded".into());
    }
    Ok(bytes)
}

// Match the existing comparator's plain-decimal input domain without converting
// the caller's text through a float (and without implementing any arithmetic).
fn plain_decimal(text: &str) -> bool {
    let unsigned = text.strip_prefix('-').unwrap_or(text);
    let mut parts = unsigned.split('.');
    let integer = parts.next().unwrap_or("");
    let fraction = parts.next();
    let digits = |part: &str| {
        (1..=30).contains(&part.len()) && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    digits(integer) && fraction.is_none_or(digits) && parts.next().is_none()
}

fn text<'a>(row: &'a Observation, key: &str) -> Result<&'a str, Error> {
    match field(row, key) {
        Some(FieldValue::Text(value)) => Ok(value),
        _ => Err(format!("missing original text field: {key}").into()),
    }
}

fn integer(row: &Observation, key: &str) -> Result<i64, Error> {
    match field(row, key) {
        Some(FieldValue::I64(value)) if *value >= 0 => Ok(*value),
        _ => Err(format!("missing nonnegative integer field: {key}").into()),
    }
}

fn main() -> Result<(), Error> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 5 || args[3] != "--reference-m" {
        return Err(
            "usage: export_tlog_nav_csv PROFILE.toml INPUT.tlog --reference-m DECIMAL".into(),
        );
    }
    let reference = &args[4];
    if !plain_decimal(reference) {
        return Err(
            "reference must be a plain decimal with at most 30 integer and 30 fractional digits"
                .into(),
        );
    }
    let profile_bytes = bounded_file(&args[1], 64 * 1024)?;
    let profile = parse_profile(std::str::from_utf8(&profile_bytes)?, "development")
        .map_err(|error| format!("profile: {error:?}"))?;
    if profile.fields.time != "host_recorded_us"
        || profile.default_clock_basis != ClockBasis::Unknown
        || profile.source_role != SourceRole::Gcs
        || profile.declared_sender.is_none()
    {
        return Err("profile must explicitly select a reported sender and GCS host_recorded_us with Unknown clock".into());
    }
    let bytes = bounded_file(&args[2], LIMIT)?;
    let observations = TlogReader
        .read(&profile, &bytes)
        .map_err(|error| format!("input: {error:?}"))?;
    let first = observations.first().ok_or("no selected observations")?;
    let selected = integer(first, "MAVLINK.selected_sender_records")?;
    let unselected = integer(first, "MAVLINK.unselected_sender_records")?;
    let input_digest = format!("sha256:{:x}", Sha256::digest(&bytes));
    let profile_digest = format!("sha256:{:x}", Sha256::digest(&profile_bytes));
    let mut output = String::from(HEADER);
    for (index, row) in observations.iter().enumerate() {
        let time = text(row, "MAVLINK.host_recorded_us")?;
        if time.is_empty() || !time.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err("invalid original host_recorded_us".into());
        }
        let system = integer(row, "MAVLINK.system_id")?;
        let component = integer(row, "MAVLINK.component_id")?;
        let message = integer(row, "MAVLINK.message_id")?;
        let raw = text(row, "MAVLINK.raw_frame_hex")?;
        if raw.is_empty() || !raw.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err("invalid original raw frame hex".into());
        }
        let value = if message == 62 {
            match field(row, "NAV_CONTROLLER_OUTPUT.xtrack_error_m_reported") {
                Some(FieldValue::F64(value)) if value.is_finite() => value.to_string(),
                _ => return Err("missing finite reported cross-track value".into()),
            }
        } else {
            String::new() // Preserve non62 records as NOT_REPORTED, not as zero.
        };
        if message == 62 && !plain_decimal(&value) {
            return Err("reported cross-track is outside the comparison decimal domain".into());
        }
        let digest: String = row
            .digest
            .0
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        output.push_str(&format!(
            "{time},{value},{reference},{input_digest},{profile_digest},{index},sha256:{digest},{system},{component},{message},{raw},{selected},{unselected},{VALUE_BASIS},{CONDITION_BASIS}\n"
        ));
        if output.len() > LIMIT {
            return Err("export CSV byte bound exceeded".into());
        }
    }
    // All rows and the native recording have succeeded before any stdout bytes.
    std::io::stdout().lock().write_all(output.as_bytes())?;
    Ok(())
}
