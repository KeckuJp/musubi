use musubi_field_contracts::{
    ClockBasisV1, DigestV1, ObservationExtensionV1, ObservationValueV1, RecordV1, decode_ndjson,
};
use std::io::Read;
use std::path::{Path, PathBuf};

pub struct DetachedRecording {
    pub records: Vec<ObservationExtensionV1>,
    pub digest: String,
    pub input: PathBuf,
}

pub fn load(path: &Path) -> Result<DetachedRecording, String> {
    if path.extension().and_then(|ext| ext.to_str()) != Some("ndjson") {
        return Err("expected an exact .ndjson file, not a transport capture".into());
    }
    const MAX_BYTES: u64 = 16 * 1024 * 1024;
    let file = std::fs::File::open(path).map_err(|error| format!("{}: {error}", path.display()))?;
    if !file
        .metadata()
        .map_err(|error| error.to_string())?
        .is_file()
    {
        return Err("expected a regular recorded file".into());
    }
    let mut bytes = Vec::new();
    file.take(MAX_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if bytes.len() as u64 > MAX_BYTES {
        return Err("detached recording exceeds 16 MiB".into());
    }
    let decoded = decode_ndjson(&bytes).map_err(|error| error.to_string())?;
    let mut records = Vec::new();
    for record in decoded {
        let RecordV1::ObservationExtension(observation) = record else {
            return Err(
                "detached observation view cannot display another record type; nothing was skipped"
                    .into(),
            );
        };
        records.push(observation);
    }
    if records.is_empty() {
        return Err("detached recording is empty".into());
    }
    Ok(DetachedRecording {
        records,
        digest: DigestV1::sha256(&bytes).as_str().to_owned(),
        input: path.to_path_buf(),
    })
}

pub fn value_label(record: &ObservationExtensionV1) -> String {
    if let Some(reason) = &record.missing {
        return format!("ABSENT: {reason:?}");
    }
    let value = match &record.value {
        Some(ObservationValueV1::Bool(value)) => value.to_string(),
        Some(ObservationValueV1::I64(value)) => value.to_string(),
        Some(ObservationValueV1::U64(value)) => value.to_string(),
        Some(ObservationValueV1::F64(value)) => value.to_string(),
        Some(ObservationValueV1::Text(value)) => value.clone(),
        None => "UNKNOWN".into(),
    };
    format!(
        "{value} {}",
        record.unit.as_deref().unwrap_or("(unit unspecified)")
    )
}

pub fn time_label(record: &ObservationExtensionV1) -> String {
    let basis = match record.clock_basis {
        ClockBasisV1::GpsLocked => "gps_locked",
        ClockBasisV1::GpsSuspect => "gps_suspect",
        ClockBasisV1::RtcSetOnce => "rtc_set_once",
        ClockBasisV1::BootRelativeOffsetEstimated => "boot_relative_offset_estimated",
        ClockBasisV1::HostReceived => "host_received",
        ClockBasisV1::BootRelative => "boot_relative",
        ClockBasisV1::Unknown => "unknown",
    };
    format!("{basis}: {} ns", record.event_time_ns)
}

#[path = "detached_tests.rs"]
#[cfg(test)]
mod detached_tests;
