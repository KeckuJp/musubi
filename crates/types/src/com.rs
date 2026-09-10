//! Common Object Model (COM): the generic observation types.
//!
//! Pure data. Nothing in this module carries a method, so no value defined here can
//! act on anything; a normalizer fills the fields and a serializer reads them.

use std::collections::BTreeMap;

/// Observation time and reception time held apart, with a confidence for the former.
///
/// ⚠️ **Unit contract**: the two fields carry **different units**. Do not unify them;
/// convert at the point of use.
/// - `observed_at`: epoch **microseconds** (the convention the adapters emit).
/// - `received_at`: epoch **milliseconds** (the convention the evidence envelope and the
///   ISO 8601 conversions on the reading side use).
///
/// Treating the two at the same precision without converting is a factor-of-1000 error.
/// A reader that prefers `observed_at` must apply `observed_at.div_euclid(1000)` before
/// comparing it with, or substituting it for, `received_at`.
#[derive(Debug, Clone, PartialEq)]
pub struct Timestamps {
    /// The time the sensor or platform claims it observed. May be untrustworthy.
    /// Epoch **microseconds**.
    pub observed_at: Option<i64>,
    /// The time this node received the observation. Epoch **milliseconds**.
    ///
    /// This is the COM/Evidence-level record of when this node received the observation,
    /// and it is not the same thing as the ingest-layer reception time held outside this
    /// crate (`musubi_core::RawObservation::received_at`). A normalizer transcribes the
    /// ingest value into this field; writing a fixed `0` here is a contract violation,
    /// because it silently replaces a measured time with a constant.
    pub received_at: i64,
    /// Confidence in the time, `0.0..=1.0`. Lower it and raise a MARK rather than
    /// presenting a suspect time as a good one.
    pub time_confidence: f32,
}

/// Latitude, longitude and altitude (WGS-84).
#[derive(Debug, Clone, PartialEq)]
pub struct Position {
    pub lat_deg: f64,
    pub lon_deg: f64,
    pub alt_m: Option<f64>,
}

/// The physical domain a platform moves in.
///
/// **Adapter-supplied**: the value is set by the adapter in `normalize()`, because the
/// adapter is what knows the class of platform it is connected to. A downstream reader
/// does not infer it.
///
/// `Unknown` is the default and means **explicitly undeclared**: an adapter that has not
/// declared a domain yet. It must never be quietly resolved into `Air`, `Surface` or
/// `Ground`. Silently resolving it is how a reader ends up drawing an airborne platform as
/// a ground one; a reader maps `Unknown` to its own distinct rendering instead.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum PlatformDomain {
    /// Airborne: uncrewed aircraft and the like.
    Air,
    /// On the water: uncrewed surface vessels and the like.
    Surface,
    /// On the ground: uncrewed ground vehicles, fixed sensors and the like.
    Ground,
    /// Domain not declared by the adapter.
    #[default]
    Unknown,
}

/// COM: the state of a platform (position and mode).
#[derive(Debug, Clone, PartialEq)]
pub struct PlatformState {
    pub platform_id: String,
    pub position: Option<Position>,
    pub mode: Option<String>,
    pub timestamps: Timestamps,
    /// The platform's physical domain, supplied by the adapter.
    pub platform_domain: PlatformDomain,
}

/// A monotonic host counter and, when a preceding observation is known, the delta from it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostStateCounter {
    pub raw: u64,
    pub delta: Option<u64>,
}

/// The narrow operating-system identity the host-state source contract allows.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HostStateOsIdentity {
    pub id: Option<String>,
    pub version_id: Option<String>,
}

/// Host load averages, carried without conversion into vehicle semantics.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HostStateLoad {
    pub one_min: Option<f64>,
    pub five_min: Option<f64>,
    pub fifteen_min: Option<f64>,
}

/// The memory values the source contract allows, all in KiB.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HostStateMemory {
    pub mem_total: Option<u64>,
    pub mem_available: Option<u64>,
    pub swap_total: Option<u64>,
    pub swap_free: Option<u64>,
}

/// Host-local values. This is inventory and health evidence, not vehicle state.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HostStateHost {
    pub os: HostStateOsIdentity,
    pub kernel_release: Option<String>,
    pub model: Option<String>,
    pub uptime_seconds: Option<f64>,
    pub load: HostStateLoad,
    pub memory_kib: HostStateMemory,
    pub cpu_total_ticks: BTreeMap<String, HostStateCounter>,
}

/// The state of one host network interface the source contract allows.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HostStateNetworkInterface {
    pub ifindex: Option<u32>,
    pub operstate: Option<String>,
    pub carrier: Option<bool>,
    pub mtu: Option<u32>,
    pub counters: BTreeMap<String, HostStateCounter>,
}

/// COM: a typed host-state observation.
///
/// Deliberately not a [`PlatformState`]: host inventory has no vehicle position and no mode,
/// and coercing it into that shape would silently drop the source contract's fields.
#[derive(Debug, Clone, PartialEq)]
pub struct HostStateObservation {
    pub source_contract_id: String,
    pub host_id: String,
    pub sequence: u64,
    pub timestamps: Timestamps,
    pub host: HostStateHost,
    pub network: BTreeMap<String, HostStateNetworkInterface>,
}

/// COM: a tracked object (a detection or a track).
#[derive(Debug, Clone, PartialEq)]
pub struct Track {
    pub track_id: String,
    pub position: Option<Position>,
    pub classification: Option<String>,
    pub timestamps: Timestamps,
}

/// COM: the description of a payload or sensor feed (video and its metadata, and so on).
#[derive(Debug, Clone, PartialEq)]
pub struct PayloadFeed {
    pub feed_id: String,
    pub kind: String,
    pub has_klv: bool,
    pub timestamps: Timestamps,
}

/// COM: the health of a platform or of one of its components.
#[derive(Debug, Clone, PartialEq)]
pub struct Health {
    pub source_id: String,
    pub status: String,
    pub timestamps: Timestamps,
}
