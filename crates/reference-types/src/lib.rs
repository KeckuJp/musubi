#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::too_many_lines,
    clippy::missing_const_for_fn,
    clippy::module_name_repetitions
)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

use std::collections::BTreeMap;

pub use musubi_types::{ConfidenceBasis, Mark, MarkStatus};

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

    #[must_use]
    pub const fn platform_domain(self) -> musubi_types::PlatformDomain {
        use musubi_types::PlatformDomain as D;
        match self {
            Self::Unknown => D::Unknown,
            Self::Ugv => D::Ground,
            Self::FixedWing | Self::Fpv => D::Air,
        }
    }
}

pub const DOMAIN_BASIS_DECLARED: &str = "profile_declared_platform_domain";
pub const DOMAIN_BASIS_FAMILY: &str = "profile_declared_family";

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
pub struct ExpectationModel {
    pub expectation_id: String,
    pub channel: ChannelId,
    pub cadence_ms: u64,
    pub grace_k: u32,
    pub frozen_ms: Option<u64>,
    pub frozen_fields: Option<Vec<String>>,
    pub required_fields: Option<Vec<String>>,
    pub declared_window_only: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SenderSelection {
    pub system_id: u8,
    pub component_id: u8,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FamilyProfile {
    pub profile_id: String,
    pub version: String,
    pub family: Family,
    pub source_role: SourceRole,
    pub format: String,
    pub extensions: Vec<String>,
    pub fields: FieldMapping,
    pub field_units: BTreeMap<String, String>,
    pub channels: Vec<ChannelId>,
    pub expectations: Vec<ExpectationModel>,
    pub default_clock_basis: ClockBasis,
    pub declared_platform_domain: Option<musubi_types::PlatformDomain>,
    pub declared_sender: Option<SenderSelection>,
    pub declared_clock_rate: Option<ClockRateConfig>,
    pub origin: String,
}

impl FamilyProfile {
    #[must_use]
    pub fn configured_platform_domain(&self) -> (musubi_types::PlatformDomain, &'static str) {
        match self.declared_platform_domain {
            Some(declared) if matches!(self.family, Family::Unknown) => {
                (declared, DOMAIN_BASIS_DECLARED)
            }
            _ => (self.family.platform_domain(), DOMAIN_BASIS_FAMILY),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct DigestRef(pub [u8; 32]);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subject {
    pub family: Family,
    pub asset_id: String,
    pub channel: ChannelId,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NegativeObservation {
    pub subject: Subject,
    pub expectation_id: String,
    pub absent_since_ms: i64,
    pub expected_by_ms: i64,
    pub last_good: Option<DigestRef>,
    pub clock_basis: ClockBasis,
    pub time_confidence_min: f32,
    pub mark: Mark,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimKind {
    Candidate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum LogicConfidence {
    NoBasis,
    LeadingIndicator,
    DirectEvidence,
    DeterministicFact,
}

impl LogicConfidence {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::NoBasis => "no_basis",
            Self::LeadingIndicator => "leading_indicator",
            Self::DirectEvidence => "direct_evidence",
            Self::DeterministicFact => "deterministic_fact",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnknownReason {
    InsufficientEvidence,
    ChannelNotObserved,
    TimeUntrusted,
    NoSignatureMatch,
    SelfLivenessLost,
}

impl UnknownReason {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::InsufficientEvidence => "insufficient-evidence",
            Self::ChannelNotObserved => "channel-not-observed",
            Self::TimeUntrusted => "time-untrusted",
            Self::NoSignatureMatch => "no-signature-match",
            Self::SelfLivenessLost => "self-liveness-lost",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SignatureMatch {
    pub matched_cues: Vec<String>,
    pub required_cues: Vec<String>,
    pub optional_cues_matched: Vec<String>,
    pub contradicting_cues: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CauseCandidate {
    pub signature_id: String,
    pub signature_match: SignatureMatch,
    pub logic_confidence: LogicConfidence,
    pub supporting: Vec<DigestRef>,
    pub contradicting: Vec<DigestRef>,
    pub discriminating_missing: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CauseOutcome {
    Consistent {
        candidates: Vec<CauseCandidate>,
    },
    Ambiguous {
        candidates: Vec<CauseCandidate>,
        indistinguishable_by: Vec<String>,
    },
    Unknown {
        reason: UnknownReason,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct BasisMix {
    pub adapter_assigned: u32,
    pub measured: u32,
    pub unspecified: u32,
}

impl BasisMix {
    pub const fn count(&mut self, basis: Option<&ConfidenceBasis>) {
        match basis {
            Some(ConfidenceBasis::AdapterAssigned) => self.adapter_assigned += 1,
            Some(ConfidenceBasis::Measured { .. }) => self.measured += 1,
            None => self.unspecified += 1,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ChannelCoverage {
    pub required: Vec<ChannelId>,
    pub present: Vec<ChannelId>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct InputConfidenceRef {
    pub envelope_digests: Vec<DigestRef>,
    pub min_confidence: f32,
    pub basis_mix: BasisMix,
    pub time_confidence_min: f32,
    pub channel_coverage: ChannelCoverage,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GeneratorRef {
    pub catalog_version: String,
    pub profile_version: String,
    pub build_id: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CauseClaim {
    pub claim_id: String,
    pub kind: ClaimKind,
    pub subject: Subject,
    pub absence: Vec<DigestRef>,
    pub outcome: CauseOutcome,
    pub input_confidence: InputConfidenceRef,
    pub evidence: Vec<DigestRef>,
    pub generator: GeneratorRef,
    pub generated_at_ms: i64,
    pub mark: Mark,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FailureKind {
    RcLinkLoss,
    TelemetryLinkLoss,
    FiberBreak,
    FcFailure,
    CameraStop,
    GnssDegradation,
    GnssInconsistency,
}

impl FailureKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::RcLinkLoss => "rc_link_loss",
            Self::TelemetryLinkLoss => "telemetry_link_loss",
            Self::FiberBreak => "fiber_break",
            Self::FcFailure => "fc_failure",
            Self::CameraStop => "camera_stop",
            Self::GnssDegradation => "gnss_degradation",
            Self::GnssInconsistency => "gnss_inconsistency",
        }
    }

    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "rc_link_loss" => Some(Self::RcLinkLoss),
            "telemetry_link_loss" => Some(Self::TelemetryLinkLoss),
            "fiber_break" => Some(Self::FiberBreak),
            "fc_failure" => Some(Self::FcFailure),
            "camera_stop" => Some(Self::CameraStop),
            "gnss_degradation" => Some(Self::GnssDegradation),
            "gnss_inconsistency" => Some(Self::GnssInconsistency),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cue {
    pub channel: ChannelId,
    pub cue_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailureSignature {
    pub signature_id: String,
    pub kind: FailureKind,
    pub families: Vec<Family>,
    pub required_cues: Vec<Cue>,
    pub optional_cues: Vec<Cue>,
    pub contradicting_cues: Vec<Cue>,
    pub discriminators: Vec<String>,
    pub consistent_with: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SignatureCatalog {
    pub version: String,
    pub signatures: Vec<FailureSignature>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OffsetSource {
    InLogAnchor,
    CrossLogEvent,
    Interpolated,
}

impl OffsetSource {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::InLogAnchor => "in_log_anchor",
            Self::CrossLogEvent => "cross_log_event",
            Self::Interpolated => "interpolated",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OffsetEstimate {
    pub offset_us: i64,
    pub bound_us: i64,
    pub source: OffsetSource,
    pub anchors: u32,
    pub discontinuity: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeAlignment {
    pub basis: ClockBasis,
    pub offset: Option<OffsetEstimate>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderRelation {
    Before,
    After,
    OrderUnknown,
}

impl OrderRelation {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::Before => "before",
            Self::After => "after",
            Self::OrderUnknown => "order-unknown",
        }
    }
}

#[must_use]
pub const fn order_with_bounds(
    a_us: i64,
    a_bound_us: i64,
    b_us: i64,
    b_bound_us: i64,
) -> OrderRelation {
    if a_us.saturating_add(a_bound_us) < b_us.saturating_sub(b_bound_us) {
        OrderRelation::Before
    } else if a_us.saturating_sub(a_bound_us) > b_us.saturating_add(b_bound_us) {
        OrderRelation::After
    } else {
        OrderRelation::OrderUnknown
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClockRateConfig {
    pub anchor_uncertainty_us: i64,
    pub min_span_us: i64,
    pub max_abs_rate_ppm: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockRateClass {
    WithinDeclaredLimit,
    OutsideDeclaredLimit,
    Indeterminate,
}

impl ClockRateClass {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::WithinDeclaredLimit => "within-declared-limit",
            Self::OutsideDeclaredLimit => "outside-declared-limit",
            Self::Indeterminate => "indeterminate",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockRateGap {
    FewerThanTwoAnchors,
    SpanShorterThanDeclaredMinimum,
    UncertaintyNotSmallerThanSpan,
    OffsetDiscontinuity,
    DeviceClockNotMonotonic,
    OffsetSourceNotInLogAnchor,
    ClockBasisNotDeviceClock,
    OutOfRepresentableRange,
}

impl ClockRateGap {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::FewerThanTwoAnchors => "fewer-than-two-anchors",
            Self::SpanShorterThanDeclaredMinimum => "span-shorter-than-declared-minimum",
            Self::UncertaintyNotSmallerThanSpan => "uncertainty-not-smaller-than-span",
            Self::OffsetDiscontinuity => "offset-discontinuity",
            Self::DeviceClockNotMonotonic => "device-clock-not-monotonic",
            Self::OffsetSourceNotInLogAnchor => "offset-source-not-in-log-anchor",
            Self::ClockBasisNotDeviceClock => "clock-basis-not-device-clock",
            Self::OutOfRepresentableRange => "out-of-representable-range",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockRateOutcome {
    Observed {
        low_ppm: i64,
        high_ppm: i64,
        class: ClockRateClass,
    },
    InsufficientBasis(ClockRateGap),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClockRateReport {
    pub outcome: ClockRateOutcome,
    pub anchors: u32,
    pub span_us: i64,
    pub config: ClockRateConfig,
}

pub mod presence {
    pub const HEADER: &str = "file,size_bytes,mtime_unix_ms,nominal_bytes_per_s";

    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Segment {
        pub file: String,
        pub size_bytes: u64,
        pub mtime_unix_ms: i64,
        pub nominal_bytes_per_s: u64,
    }

    impl Segment {
        #[must_use]
        pub fn start_unix_ms(&self) -> i64 {
            self.mtime_unix_ms - (self.size_bytes * 1000 / self.nominal_bytes_per_s.max(1)) as i64
        }
    }

    pub fn parse_csv(text: &str) -> Result<Vec<Segment>, usize> {
        let mut lines = text.lines().enumerate();
        let (_, h) = lines.next().ok_or(0usize)?;
        if h.trim() != HEADER {
            return Err(0);
        }
        let mut out = Vec::new();
        for (ln, line) in lines {
            if line.trim().is_empty() {
                continue;
            }
            let c: Vec<&str> = line.split(',').map(str::trim).collect();
            if c.len() < 4 {
                return Err(ln);
            }
            out.push(Segment {
                file: c[0].to_string(),
                size_bytes: c[1].parse().map_err(|_| ln)?,
                mtime_unix_ms: c[2].parse().map_err(|_| ln)?,
                nominal_bytes_per_s: c[3].parse().map_err(|_| ln)?,
            });
        }
        Ok(out)
    }

    #[must_use]
    pub fn render_csv(segments: &[Segment]) -> Vec<u8> {
        let mut s = String::from(HEADER);
        s.push('\n');
        for g in segments {
            s.push_str(&format!(
                "{},{},{},{}\n",
                g.file, g.size_bytes, g.mtime_unix_ms, g.nominal_bytes_per_s
            ));
        }
        s.into_bytes()
    }
}

#[must_use]
pub fn join_reason_tokens(tokens: &[&str]) -> String {
    assert!(
        tokens.iter().all(|t| !t.contains(';')),
        "reason token must not contain ';' (reason-delimiter)"
    );
    tokens.join("|")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clock_basis_labels_round_trip() {
        for b in [
            ClockBasis::GpsLocked,
            ClockBasis::GpsSuspect,
            ClockBasis::RtcSetOnce,
            ClockBasis::BootRelativeOffsetEstimated,
            ClockBasis::HostReceived,
            ClockBasis::BootRelative,
            ClockBasis::Unknown,
        ] {
            assert_eq!(ClockBasis::parse(b.as_label()), Some(b));
        }
        assert_eq!(ClockBasis::parse("gps"), None);
    }

    #[test]
    fn channel_role_family_labels_round_trip() {
        for c in [
            ChannelId::Onboard,
            ChannelId::Tlog,
            ChannelId::LinkStats,
            ChannelId::Rc,
            ChannelId::Heartbeat,
            ChannelId::GpsEkf,
            ChannelId::Event,
            ChannelId::Video,
            ChannelId::Osd,
            ChannelId::Fiber,
        ] {
            assert_eq!(ChannelId::parse(c.as_str()), Some(c));
        }
        for r in [
            SourceRole::Fc,
            SourceRole::Gcs,
            SourceRole::Handset,
            SourceRole::Video,
        ] {
            assert_eq!(SourceRole::parse(r.as_str()), Some(r));
        }
        for f in [Family::Ugv, Family::FixedWing, Family::Fpv] {
            assert_eq!(Family::parse(f.as_str()), Some(f));
        }
        for k in [
            FailureKind::RcLinkLoss,
            FailureKind::TelemetryLinkLoss,
            FailureKind::FiberBreak,
            FailureKind::FcFailure,
            FailureKind::CameraStop,
            FailureKind::GnssDegradation,
            FailureKind::GnssInconsistency,
        ] {
            assert_eq!(FailureKind::parse(k.as_str()), Some(k));
        }
        assert_eq!(FailureKind::parse("nope"), None);
    }

    #[test]
    fn reason_tokens_join_with_pipe_never_semicolon() {
        let s = join_reason_tokens(&[
            "absence-expected-cadence",
            &ClockBasis::HostReceived.reason_token(),
        ]);
        assert_eq!(s, "absence-expected-cadence|clock-basis:host_received");
        assert!(!s.contains(';'));
    }

    #[test]
    #[should_panic(expected = "reason-delimiter")]
    fn reason_token_with_semicolon_is_rejected() {
        let _ = join_reason_tokens(&["a;b"]);
    }

    #[test]
    fn logic_confidence_ladder_is_ordered_and_unknown_is_first_class() {
        assert!(LogicConfidence::NoBasis < LogicConfidence::DeterministicFact);
        let o = CauseOutcome::Unknown {
            reason: UnknownReason::ChannelNotObserved,
        };
        assert!(matches!(o, CauseOutcome::Unknown { .. }));
        assert_eq!(
            UnknownReason::ChannelNotObserved.as_label(),
            "channel-not-observed"
        );
    }

    #[test]
    fn overlapping_error_bands_never_claim_an_order() {
        assert_eq!(
            order_with_bounds(0, 1_000, 5_000, 1_000),
            OrderRelation::Before
        );
        assert_eq!(
            order_with_bounds(5_000, 1_000, 0, 1_000),
            OrderRelation::After
        );
        assert_eq!(
            order_with_bounds(0, 3_000, 5_000, 3_000),
            OrderRelation::OrderUnknown
        );
        assert_eq!(OrderRelation::OrderUnknown.as_label(), "order-unknown");
    }

    #[test]
    fn basis_mix_counts_existing_confidence_basis() {
        let mut m = BasisMix::default();
        m.count(Some(&ConfidenceBasis::AdapterAssigned));
        m.count(Some(&ConfidenceBasis::Measured {
            calibration_id: None,
        }));
        m.count(None);
        assert_eq!((m.adapter_assigned, m.measured, m.unspecified), (1, 1, 1));
    }
}
