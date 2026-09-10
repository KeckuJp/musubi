//! The seven synthetic civil sources, and the observations they emit.
//!
//! ## What a source is here
//!
//! Each source produces [`RawObservation`]s on a text wire and normalizes them into sealed
//! [`EvidenceEnvelope`]s carrying a COM [`PlatformState`]. The wire is the same
//! whitespace-delimited shape the adapter SPI's worked template uses, so a reader who has
//! read one has read both.
//!
//! ## The narrowness, stated plainly
//!
//! The COM object enumeration currently carries one variant, `PlatformState`. So every source
//! here -- including the four that are sensors rather than vehicles -- produces evidence about
//! **a platform**, and the sensor reading rides in the platform's `mode` and in the MARK
//! provenance rather than in a payload or health object of its own.
//!
//! That is a real limitation and not a modelling preference. A weather station and an RTK base
//! are stationary ground platforms with a position and a state, so they fit; a camera and a
//! multispectral head are payloads on a carrier, so their evidence is the carrier's state with
//! the payload reading attached. When the COM grows the variants for tracks, payload feeds and
//! health, these sources gain them, and until then this pack does not pretend they exist.
//!
//! ## No field origin
//!
//! Every value comes from [`crate::synth`]. Nothing here was recorded anywhere, and nothing
//! here is a capture with its identifying fields swapped out.

use crate::synth::{EPOCH_START_S, FIELD_LAT, FIELD_LON, INGEST_RECEIVED_AT_MS, SplitMix64};
use musubi_core::{
    NormalizeError, ObservationQuality, QualityNote, RawObservation, derive_mark, seal_digest,
};
use musubi_types::{
    Claim, ComObject, ConfidenceBasis, EvidenceEnvelope, Mark, MarkStatus, PlatformDomain,
    PlatformState, Position, Timestamps,
};
use std::fmt;

/// The seven source kinds this pack covers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum SourceKind {
    /// An agricultural uncrewed aircraft flying a survey pattern.
    AgriculturalUav,
    /// An agricultural uncrewed ground vehicle working a row.
    AgriculturalUgv,
    /// A visual camera payload carried by the aircraft.
    Camera,
    /// A multispectral payload carried by the aircraft.
    Multispectral,
    /// A stationary RTK base station.
    RtkBase,
    /// A stationary weather station.
    WeatherStation,
    /// The carrier's own health telemetry.
    Health,
}

impl SourceKind {
    /// Every kind, in a fixed order, so that a caller iterating them gets a stable pack.
    pub const ALL: [Self; 7] = [
        Self::AgriculturalUav,
        Self::AgriculturalUgv,
        Self::Camera,
        Self::Multispectral,
        Self::RtkBase,
        Self::WeatherStation,
        Self::Health,
    ];

    /// The adapter slug this kind reports in provenance.
    #[must_use]
    pub const fn slug(self) -> &'static str {
        match self {
            Self::AgriculturalUav => "civil/agri-uav-text/v1",
            Self::AgriculturalUgv => "civil/agri-ugv-text/v1",
            Self::Camera => "civil/camera-text/v1",
            Self::Multispectral => "civil/multispectral-text/v1",
            Self::RtkBase => "civil/rtk-base-text/v1",
            Self::WeatherStation => "civil/weather-text/v1",
            Self::Health => "civil/health-text/v1",
        }
    }

    /// The physical domain of the platform this kind reports about.
    ///
    /// The aircraft and its two payloads are airborne; the ground vehicle, the base station
    /// and the weather station are on the ground. Health telemetry is about the carrier, so it
    /// declares nothing rather than guessing which carrier it came from.
    #[must_use]
    pub const fn domain(self) -> PlatformDomain {
        match self {
            Self::AgriculturalUav | Self::Camera | Self::Multispectral => PlatformDomain::Air,
            Self::AgriculturalUgv | Self::RtkBase | Self::WeatherStation => PlatformDomain::Ground,
            Self::Health => PlatformDomain::Unknown,
        }
    }

    /// Whether this kind stays in one place.
    ///
    /// A stationary source repeats its position exactly, which is what makes a drift in a base
    /// station's reported position visible instead of ordinary movement.
    #[must_use]
    pub const fn is_stationary(self) -> bool {
        matches!(self, Self::RtkBase | Self::WeatherStation)
    }
}

impl fmt::Display for SourceKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.slug())
    }
}

/// One synthetic observation before it is put on a wire.
///
/// Public so that a reader can compare what the pack intended to emit against what the
/// normalizer produced, rather than comparing the pipeline with itself.
#[derive(Debug, Clone, PartialEq)]
pub struct SynthObservation {
    /// Which source produced it.
    pub kind: SourceKind,
    /// The identifier of the tap that received it.
    pub source_id: String,
    /// Latitude, WGS-84 decimal degrees.
    pub lat: f64,
    /// Longitude, WGS-84 decimal degrees.
    pub lon: f64,
    /// Altitude above MSL in metres, when the source reports one.
    pub alt_m: Option<f64>,
    /// The observation time in epoch seconds, when the source reports one.
    pub ts_epoch_s: Option<i64>,
    /// The source's declared operating state, for example `"survey"` or `"charging"`.
    pub mode: &'static str,
    /// The source's own reading, as `(name, value)` pairs. Carried into provenance.
    ///
    /// A name here never contains `';'`: that is the top-level separator on the reading side,
    /// and a `';'` inside a provenance token cannot be split apart again.
    pub readings: Vec<(&'static str, f64)>,
    /// Whether this observation is degraded at source, for example a weak fix or a low
    /// battery. A degraded observation is still emitted; it is never dropped.
    pub degraded: Option<&'static str>,
}

impl SynthObservation {
    /// Render the observation as one wire line.
    ///
    /// The shape is `lat lon alt ts mode name=value...`, with `-` as the explicit absent
    /// token so that a missing optional column never silently shifts the ones after it.
    #[must_use]
    pub fn to_wire(&self) -> String {
        let mut line = format!("{:.6} {:.6} ", self.lat, self.lon);
        match self.alt_m {
            Some(alt) => line.push_str(&format!("{alt:.2} ")),
            None => line.push_str("- "),
        }
        match self.ts_epoch_s {
            Some(ts) => line.push_str(&format!("{ts} ")),
            None => line.push_str("- "),
        }
        line.push_str(self.mode);
        for (name, value) in &self.readings {
            line.push_str(&format!(" {name}={value:.3}"));
        }
        line
    }

    /// Wrap the wire line as a raw observation, stamped with the pack's ingest time.
    #[must_use]
    pub fn to_raw(&self) -> RawObservation {
        RawObservation {
            source_id: self.source_id.clone(),
            payload: self.to_wire().into_bytes(),
            received_at: INGEST_RECEIVED_AT_MS,
        }
    }

    /// Normalize the observation into a sealed evidence envelope.
    ///
    /// The status is never set here: quality facts are collected as notes and
    /// [`derive_mark`] decides OK versus DEGRADED, exactly as an adapter must.
    #[must_use]
    pub fn normalize(&self) -> EvidenceEnvelope {
        let mut notes: Vec<QualityNote> = Vec::new();

        if self.alt_m.is_some() {
            notes.push(QualityNote::neutral("alt-ref:MSL-assumed"));
        } else {
            notes.push(QualityNote::degraded(
                "alt field absent",
                "field-missing:alt",
            ));
        }
        notes.push(QualityNote::neutral(format!("mode:{}", self.mode)));
        for (name, value) in &self.readings {
            notes.push(QualityNote::neutral(format!("{name}:{value:.3}")));
        }
        if let Some(reason) = self.degraded {
            notes.push(QualityNote::degraded(
                "source reports a degradation",
                reason,
            ));
        }
        notes.push(QualityNote::neutral("confidence:adapter-assigned"));

        let (observed_at, time_confidence) = match self.ts_epoch_s {
            Some(ts) => (Some(ts * 1_000_000), 0.75_f32),
            None => (None, 0.2_f32),
        };

        let platform = PlatformState {
            platform_id: self.source_id.clone(),
            position: Some(Position {
                lat_deg: self.lat,
                lon_deg: self.lon,
                alt_m: self.alt_m,
            }),
            mode: Some(self.mode.to_string()),
            timestamps: Timestamps {
                observed_at,
                // Transcribed from the ingest layer, never replaced with a constant.
                received_at: INGEST_RECEIVED_AT_MS,
                time_confidence,
            },
            platform_domain: self.kind.domain(),
        };

        let mark = derive_mark(&ObservationQuality {
            source_id: self.source_id.clone(),
            adapter_slug: self.kind.slug().to_string(),
            time_confidence,
            notes,
        });

        seal_digest(EvidenceEnvelope {
            observation: ComObject::PlatformState(platform),
            claim: Claim {
                claim_id: format!("{}:{}", self.source_id, self.kind.slug()),
                // Adapter-assigned, not read off the wire: none of these synthetic sources
                // publishes a confidence field, and inventing one would be the fabrication
                // the basis field exists to make visible.
                confidence: 0.6,
                mark,
                confidence_basis: Some(ConfidenceBasis::AdapterAssigned),
            },
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        })
    }
}

/// An observation this pack refuses, and the reason it refuses it.
///
/// The pack carries adversarial fixtures as well as good ones, because a fixture set that only
/// contains well-formed input cannot show that anything is being checked.
#[derive(Debug, Clone, PartialEq)]
pub struct RejectedObservation {
    /// The source id the rejection is attributed to.
    pub source_id: String,
    /// The wire line that was refused.
    pub wire: String,
    /// The short reason token.
    pub reason_code: &'static str,
}

impl RejectedObservation {
    /// The normalization error this rejection produces.
    ///
    /// INVALID with a non-empty reason and the source preserved: a refusal that loses which
    /// source it came from is a refusal nobody can act on.
    #[must_use]
    pub fn to_error(&self) -> NormalizeError {
        NormalizeError {
            source_id: self.source_id.clone(),
            mark: Mark {
                status: MarkStatus::Invalid,
                reason_code: self.reason_code.to_string(),
                provenance: vec![
                    format!("source:{}", self.source_id),
                    format!("wire-len:{}", self.wire.len()),
                ],
            },
        }
    }
}

/// Generate `count` observations for one source kind from `seed`.
///
/// Deterministic in both arguments. A stationary kind repeats one position exactly; a moving
/// kind walks within the field bounds. Roughly one observation in six carries a degradation,
/// so that a consumer reading this pack sees a mixed population rather than a clean one.
///
/// # Panics
/// Panics when `count` is zero: an empty source would silently satisfy every consumer test.
#[must_use]
pub fn observations(kind: SourceKind, seed: u64, count: usize) -> Vec<SynthObservation> {
    assert!(count > 0, "a source with no observations proves nothing");
    let mut g = SplitMix64::new(seed);
    let source_id = format!("civil-{}", kind.slug().replace('/', "-"));

    let modes: &[&'static str] = match kind {
        SourceKind::AgriculturalUav => &["survey", "transit", "hover"],
        SourceKind::AgriculturalUgv => &["row-follow", "headland-turn", "idle"],
        SourceKind::Camera => &["capture", "standby"],
        SourceKind::Multispectral => &["capture", "calibrate"],
        SourceKind::RtkBase => &["fixed"],
        SourceKind::WeatherStation => &["station"],
        SourceKind::Health => &["nominal", "charging"],
    };
    let degradations: &[&'static str] = match kind {
        SourceKind::AgriculturalUav | SourceKind::AgriculturalUgv => {
            &["gnss-fix-weak", "vibration-high"]
        }
        SourceKind::Camera => &["exposure-clipped"],
        SourceKind::Multispectral => &["calibration-stale"],
        SourceKind::RtkBase => &["correction-age-high"],
        SourceKind::WeatherStation => &["sensor-icing-suspected"],
        SourceKind::Health => &["battery-low", "motor-temp-high"],
    };

    let base_lat = g.next_rounded(FIELD_LAT.0, FIELD_LAT.1, 6);
    let base_lon = g.next_rounded(FIELD_LON.0, FIELD_LON.1, 6);

    (0..count)
        .map(|i| {
            let (lat, lon) = if kind.is_stationary() {
                (base_lat, base_lon)
            } else {
                (
                    g.next_rounded(FIELD_LAT.0, FIELD_LAT.1, 6),
                    g.next_rounded(FIELD_LON.0, FIELD_LON.1, 6),
                )
            };
            let alt_m = match kind {
                SourceKind::AgriculturalUav | SourceKind::Camera | SourceKind::Multispectral => {
                    Some(g.next_rounded(20.0, 120.0, 2))
                }
                SourceKind::AgriculturalUgv | SourceKind::RtkBase | SourceKind::WeatherStation => {
                    Some(g.next_rounded(8.0, 14.0, 2))
                }
                // Health telemetry carries no altitude of its own; it is about the carrier,
                // not about a place. The absent altitude degrades, which is correct: the
                // reading really does say less than a positional one.
                SourceKind::Health => None,
            };
            // One observation in eight omits its timestamp, so that the time-suspect path is
            // exercised by the pack rather than only by a hand-written test.
            let ts_epoch_s = if g.chance(0.125) {
                None
            } else {
                Some(EPOCH_START_S + i as i64 * 5)
            };
            let readings: Vec<(&'static str, f64)> = match kind {
                SourceKind::AgriculturalUav => vec![
                    ("speed_mps", g.next_rounded(0.0, 14.0, 3)),
                    ("heading_deg", g.next_rounded(0.0, 360.0, 3)),
                ],
                SourceKind::AgriculturalUgv => vec![
                    ("speed_mps", g.next_rounded(0.0, 3.0, 3)),
                    ("implement_depth_cm", g.next_rounded(0.0, 25.0, 3)),
                ],
                SourceKind::Camera => vec![
                    ("frames_per_s", g.next_rounded(5.0, 30.0, 3)),
                    ("exposure_ms", g.next_rounded(0.5, 20.0, 3)),
                ],
                SourceKind::Multispectral => {
                    vec![("ndvi", g.next_rounded(-0.2, 0.95, 3)), ("bands", 5.0)]
                }
                SourceKind::RtkBase => vec![
                    ("correction_age_s", g.next_rounded(0.2, 8.0, 3)),
                    ("satellites", g.next_rounded(7.0, 24.0, 0)),
                ],
                SourceKind::WeatherStation => vec![
                    ("air_temp_c", g.next_rounded(-4.0, 36.0, 3)),
                    ("wind_mps", g.next_rounded(0.0, 18.0, 3)),
                    ("rain_mm_h", g.next_rounded(0.0, 12.0, 3)),
                ],
                SourceKind::Health => vec![
                    ("battery_pct", g.next_rounded(8.0, 100.0, 3)),
                    ("motor_temp_c", g.next_rounded(20.0, 95.0, 3)),
                ],
            };
            let degraded = if g.chance(1.0 / 6.0) {
                Some(*g.pick(degradations))
            } else {
                None
            };
            SynthObservation {
                kind,
                source_id: source_id.clone(),
                lat,
                lon,
                alt_m,
                ts_epoch_s,
                mode: *g.pick(modes),
                readings,
                degraded,
            }
        })
        .collect()
}

/// The adversarial fixtures: input this pack expects to be refused.
///
/// Each one names the defect it carries, so that a consumer test asserting "this many were
/// refused" is asserting something specific rather than counting.
#[must_use]
pub fn rejections() -> Vec<RejectedObservation> {
    vec![
        RejectedObservation {
            source_id: "civil-adversarial".to_string(),
            wire: String::new(),
            reason_code: "wire-malformed",
        },
        RejectedObservation {
            source_id: "civil-adversarial".to_string(),
            wire: "36.12".to_string(),
            reason_code: "wire-malformed",
        },
        RejectedObservation {
            source_id: "civil-adversarial".to_string(),
            wire: "95.0 140.08 12.0 1726300000 survey".to_string(),
            reason_code: "position-out-of-range",
        },
        RejectedObservation {
            source_id: "civil-adversarial".to_string(),
            wire: "36.12 200.0 12.0 1726300000 survey".to_string(),
            reason_code: "position-out-of-range",
        },
        RejectedObservation {
            source_id: "civil-adversarial".to_string(),
            wire: "not-a-number 140.08 12.0 1726300000 survey".to_string(),
            reason_code: "field-unparseable",
        },
    ]
}
