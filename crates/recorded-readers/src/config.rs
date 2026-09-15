//! Read-only decoding configuration. No diagnosis, profile override, or clock promotion.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Family {
    Unknown,
    Ugv,
    FixedWing,
    Fpv,
}

impl Family {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Ugv => "ugv",
            Self::FixedWing => "fixed_wing",
            Self::Fpv => "fpv",
        }
    }

    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "unknown" => Some(Self::Unknown),
            "ugv" => Some(Self::Ugv),
            "fixed_wing" => Some(Self::FixedWing),
            "fpv" => Some(Self::Fpv),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SourceRole {
    Fc,
    Gcs,
    Handset,
    Video,
    RecordedExport,
}

impl SourceRole {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Fc => "fc",
            Self::Gcs => "gcs",
            Self::Handset => "handset",
            Self::Video => "video",
            Self::RecordedExport => "recorded_export",
        }
    }

    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "fc" => Some(Self::Fc),
            "gcs" => Some(Self::Gcs),
            "handset" => Some(Self::Handset),
            "video" => Some(Self::Video),
            "recorded_export" => Some(Self::RecordedExport),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum ChannelId {
    Onboard,
    Tlog,
    LinkStats,
    Rc,
    Heartbeat,
    GpsEkf,
    Event,
    Video,
    Osd,
    Fiber,
}

impl ChannelId {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Onboard => "onboard",
            Self::Tlog => "tlog",
            Self::LinkStats => "link_stats",
            Self::Rc => "rc",
            Self::Heartbeat => "heartbeat",
            Self::GpsEkf => "gps_ekf",
            Self::Event => "event",
            Self::Video => "video",
            Self::Osd => "osd",
            Self::Fiber => "fiber",
        }
    }

    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "onboard" => Some(Self::Onboard),
            "tlog" => Some(Self::Tlog),
            "link_stats" => Some(Self::LinkStats),
            "rc" => Some(Self::Rc),
            "heartbeat" => Some(Self::Heartbeat),
            "gps_ekf" => Some(Self::GpsEkf),
            "event" => Some(Self::Event),
            "video" => Some(Self::Video),
            "osd" => Some(Self::Osd),
            "fiber" => Some(Self::Fiber),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ClockBasis {
    GpsLocked,
    GpsSuspect,
    RtcSetOnce,
    BootRelativeOffsetEstimated,
    HostReceived,
    BootRelative,
    Unknown,
}

impl ClockBasis {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::GpsLocked => "gps_locked",
            Self::GpsSuspect => "gps_suspect",
            Self::RtcSetOnce => "rtc_set_once",
            Self::BootRelativeOffsetEstimated => "boot_relative_offset_estimated",
            Self::HostReceived => "host_received",
            Self::BootRelative => "boot_relative",
            Self::Unknown => "unknown",
        }
    }

    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "gps_locked" => Some(Self::GpsLocked),
            "gps_suspect" => Some(Self::GpsSuspect),
            "rtc_set_once" => Some(Self::RtcSetOnce),
            "boot_relative_offset_estimated" => Some(Self::BootRelativeOffsetEstimated),
            "host_received" => Some(Self::HostReceived),
            "boot_relative" => Some(Self::BootRelative),
            "unknown" => Some(Self::Unknown),
            _ => None,
        }
    }

    #[must_use]
    pub fn reason_token(self) -> String {
        format!("clock-basis:{}", self.as_label())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct FieldMapping {
    pub time: String,
    pub status: Vec<String>,
    pub link: Vec<String>,
    pub battery: Vec<String>,
    pub gps: Vec<String>,
    pub health: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadProfile {
    pub profile_id: String,
    pub version: String,
    pub family: Family,
    pub source_role: SourceRole,
    pub format: String,
    pub extensions: Vec<String>,
    pub fields: FieldMapping,
    pub field_units: std::collections::BTreeMap<String, String>,
    pub channels: Vec<ChannelId>,
    pub default_clock_basis: ClockBasis,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct DigestRef(pub [u8; 32]);
