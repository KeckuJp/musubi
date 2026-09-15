#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::too_many_lines,
    clippy::missing_const_for_fn,
    clippy::module_name_repetitions,
    clippy::struct_excessive_bools,
    clippy::cognitive_complexity
)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

pub mod absence;
pub mod bin;
pub mod blackbox;
pub mod catalog;
pub mod cause;
pub mod edgetx;
pub mod eval;
mod mavlink_header;
pub mod mavlog_json;
pub mod mseed_waveform;
pub mod profile;
pub mod telemetry_csv;
pub mod time_align;
pub mod tlog;
pub mod ulog;
pub mod video;

pub use musubi_reference_types::{
    ChannelId, ClockBasis, DigestRef, ExpectationModel, FamilyProfile, NegativeObservation,
    SourceRole,
};

#[derive(Debug, Clone, PartialEq)]
pub enum FieldValue {
    I64(i64),
    F64(f64),
    Text(String),
    Blank,
}

impl FieldValue {
    pub(crate) fn float32_bits(raw: [u8; 4]) -> Self {
        let value = f32::from_le_bytes(raw);
        if value.is_finite() {
            Self::F64(f64::from(value))
        } else {
            Self::Text(format!(
                "nonfinite_float32:0x{:08x}",
                u32::from_le_bytes(raw)
            ))
        }
    }

    pub(crate) fn float64_bits(raw: [u8; 8]) -> Self {
        let value = f64::from_le_bytes(raw);
        if value.is_finite() {
            Self::F64(value)
        } else {
            Self::Text(format!(
                "nonfinite_float64:0x{:016x}",
                u64::from_le_bytes(raw)
            ))
        }
    }

    #[must_use]
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Self::I64(i) => Some(*i as f64),
            Self::F64(f) => Some(*f),
            Self::Text(_) | Self::Blank => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Observation {
    pub t_ms: i64,
    pub clock_basis: ClockBasis,
    pub time_confidence: f32,
    pub source_role: SourceRole,
    pub channel: ChannelId,
    pub fields: Vec<(String, FieldValue)>,
    pub stale: bool,
    pub digest: DigestRef,
    pub t_boot_us: Option<u64>,
    pub anchor_unix_us: Option<i64>,
    pub wall_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadError {
    NotImplemented {
        format: &'static str,
        ledger: &'static str,
    },
    UnsupportedFormat {
        format: String,
    },
    Malformed {
        offset: usize,
        what: String,
    },
    Profile(String),
}

impl std::fmt::Display for ReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotImplemented { format, ledger } => {
                write!(f, "reader for {format} not implemented ({ledger})")
            }
            Self::UnsupportedFormat { format } => write!(f, "no reader for format {format}"),
            Self::Malformed { offset, what } => {
                write!(f, "malformed input at byte {offset}: {what}")
            }
            Self::Profile(s) => write!(f, "profile error: {s}"),
        }
    }
}

impl std::error::Error for ReadError {}

pub trait ProfileReader {
    fn format_id(&self) -> &'static str;
    fn read(&self, profile: &FamilyProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError>;
}

#[must_use]
pub fn reader_for(format: &str) -> Option<Box<dyn ProfileReader>> {
    match format {
        "mavlink_tlog" => Some(Box::new(tlog::TlogReader)),
        "edgetx_csv" => Some(Box::new(edgetx::EdgeTxCsvReader)),
        "ardupilot_dataflash_bin" => Some(Box::new(bin::ArduPilotBinReader)),
        "px4_ulog" => Some(Box::new(ulog::Px4UlgReader)),
        "blackbox_decoded_csv" => Some(Box::new(blackbox::BlackboxCsvReader)),
        "telemetry_csv_us" => Some(Box::new(telemetry_csv::TelemetryCsvReader)),
        "pymavlink_dataflash_jsonl" => Some(Box::new(mavlog_json::MavlogJsonReader)),
        "video_presence" => Some(Box::new(video::VideoPresenceReader)),
        _ => None,
    }
}

#[must_use]
pub fn profiles_for_extension<'a>(
    profiles: &'a [FamilyProfile],
    ext: &str,
) -> Vec<&'a FamilyProfile> {
    let ext = ext.trim_start_matches('.').to_ascii_lowercase();
    profiles
        .iter()
        .filter(|p| p.extensions.contains(&ext))
        .collect()
}

#[must_use]
pub const fn default_time_confidence(basis: ClockBasis) -> f32 {
    match basis {
        ClockBasis::GpsLocked => 0.9,
        ClockBasis::RtcSetOnce => 0.6,
        ClockBasis::BootRelativeOffsetEstimated => 0.55,
        ClockBasis::GpsSuspect => 0.3,
        ClockBasis::HostReceived | ClockBasis::BootRelative => 0.2,
        ClockBasis::Unknown => 0.0,
    }
}

#[must_use]
pub fn observation_digest(
    t_ms: i64,
    channel: ChannelId,
    fields: &[(String, FieldValue)],
) -> DigestRef {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(t_ms.to_le_bytes());
    h.update(channel.as_str().as_bytes());
    for (k, v) in fields {
        h.update(k.as_bytes());
        h.update(b"=");
        match v {
            FieldValue::I64(i) => h.update(i.to_le_bytes()),
            FieldValue::F64(f) => h.update(f.to_bits().to_le_bytes()),
            FieldValue::Text(s) => h.update(s.as_bytes()),
            FieldValue::Blank => h.update(b"<blank>"),
        }
        h.update(b";");
    }
    DigestRef(h.finalize().into())
}

#[must_use]
pub fn is_time_like_field(name: &str) -> bool {
    if matches!(
        name,
        "MAVLINK.packet_sequence" | "MAVLINK.host_received_us" | "MAVLINK.raw_frame_hex"
    ) {
        return true;
    }
    let n = name.rsplit('.').next().unwrap_or(name);
    matches!(
        n,
        "TimeUS"
            | "TimeMS"
            | "timestamp"
            | "timestamp_last_signal"
            | "time_boot_ms"
            | "time_usec"
            | "time_unix_usec"
            | "time"
            | "loopIteration"
            | "tmr10ms"
            | "Date"
            | "Time"
            | "SMS"
            | "GMS"
    )
}

#[must_use]
pub fn value_digest(fields: &[(String, FieldValue)]) -> DigestRef {
    let v: Vec<(String, FieldValue)> = fields
        .iter()
        .filter(|(k, _)| !is_time_like_field(k))
        .cloned()
        .collect();
    observation_digest(0, ChannelId::Onboard, &v)
}

#[must_use]
pub fn observation(
    profile: &FamilyProfile,
    t_ms: i64,
    channel: ChannelId,
    fields: Vec<(String, FieldValue)>,
    stale: bool,
) -> Observation {
    let basis = profile.default_clock_basis;
    let host_axis = matches!(basis, ClockBasis::HostReceived);
    Observation {
        t_ms,
        clock_basis: basis,
        time_confidence: default_time_confidence(basis),
        source_role: profile.source_role,
        channel,
        digest: observation_digest(t_ms, channel, &fields),
        fields,
        stale,
        t_boot_us: None,
        anchor_unix_us: None,
        wall_ms: if host_axis { Some(t_ms) } else { None },
    }
}

#[must_use]
pub fn field<'a>(o: &'a Observation, name: &str) -> Option<&'a FieldValue> {
    o.fields.iter().find(|(k, _)| k == name).map(|(_, v)| v)
}
