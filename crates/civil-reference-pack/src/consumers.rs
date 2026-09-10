//! The six consumer views.
//!
//! A consumer reads sealed evidence and answers one domain question. Every function here is
//! pure: it takes a slice of envelopes and returns a summary. None of them mutates an
//! envelope, none reads a clock or a file, and none produces anything a device could act on.
//! They exist so that the pack demonstrates the kernel is useful on its own -- six civil
//! questions answered from public artifacts, with no other pack required.
//!
//! Every view is also a **ledger**: each one accounts for every envelope it was given, so a
//! reader can check that the counts add up rather than trusting that nothing was dropped.

use musubi_types::{ComObject, EvidenceEnvelope, MarkStatus, PlatformState};
use std::collections::BTreeMap;

/// Borrow the platform state out of an envelope.
///
/// The COM enumeration carries more than one variant, so this is not total, and it is
/// written as a function rather than inlined so that the compiler points at one place
/// instead of six. The pack builds every envelope it reads, and builds all of them from a
/// platform state; anything else here is a caller passing this an envelope from somewhere
/// else, which the pack has no reading for.
fn state(envelope: &EvidenceEnvelope) -> &PlatformState {
    let ComObject::PlatformState(ps) = &envelope.observation else {
        panic!(
            "the reference pack reads platform state, and this envelope carries another COM object"
        )
    };
    ps
}

/// Read one named reading back out of the MARK provenance.
///
/// The sources put their readings in provenance as `name:value`. Parsing them back is how a
/// consumer sees a sensor value without the COM carrying a variant for every sensor.
fn reading(envelope: &EvidenceEnvelope, name: &str) -> Option<f64> {
    let prefix = format!("{name}:");
    envelope
        .claim
        .mark
        .provenance
        .iter()
        .find_map(|token| token.strip_prefix(&prefix))
        .and_then(|v| v.parse().ok())
}

/// How a population of evidence divides by MARK status.
///
/// The one structure every view carries, so that "nothing was dropped" is checkable in each
/// of them by the same arithmetic.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct MarkLedger {
    /// Envelopes marked OK.
    pub ok: usize,
    /// Envelopes marked DEGRADED.
    pub degraded: usize,
    /// Envelopes marked INVALID.
    pub invalid: usize,
    /// Envelopes carrying a status no adapter may emit. Counted rather than ignored: an
    /// unexpected status is a finding, and a view that silently skipped it would hide one.
    pub reserved: usize,
}

impl MarkLedger {
    /// Tally a population.
    #[must_use]
    pub fn of(envelopes: &[EvidenceEnvelope]) -> Self {
        let mut l = Self::default();
        for e in envelopes {
            match e.claim.mark.status {
                MarkStatus::Ok => l.ok += 1,
                MarkStatus::Degraded => l.degraded += 1,
                MarkStatus::Invalid => l.invalid += 1,
                _ => l.reserved += 1,
            }
        }
        l
    }

    /// Everything accounted for.
    #[must_use]
    pub const fn total(&self) -> usize {
        self.ok + self.degraded + self.invalid + self.reserved
    }
}

/// The farm view: where the platforms were, and how much of the population is usable.
#[derive(Debug, Clone, PartialEq)]
pub struct FarmView {
    /// The ledger over everything handed in.
    pub ledger: MarkLedger,
    /// The most recent position seen per platform, by observed time.
    pub last_position_by_platform: BTreeMap<String, (f64, f64)>,
    /// The bounding box of every position seen, as `(min_lat, min_lon, max_lat, max_lon)`.
    pub bounds: Option<(f64, f64, f64, f64)>,
}

/// Where the platforms were, and how much of what came in is usable.
#[must_use]
pub fn farm_view(envelopes: &[EvidenceEnvelope]) -> FarmView {
    let mut last: BTreeMap<String, (i64, f64, f64)> = BTreeMap::new();
    let mut bounds: Option<(f64, f64, f64, f64)> = None;
    for e in envelopes {
        let ps = state(e);
        if let Some(pos) = &ps.position {
            bounds = Some(match bounds {
                None => (pos.lat_deg, pos.lon_deg, pos.lat_deg, pos.lon_deg),
                Some((min_lat, min_lon, max_lat, max_lon)) => (
                    min_lat.min(pos.lat_deg),
                    min_lon.min(pos.lon_deg),
                    max_lat.max(pos.lat_deg),
                    max_lon.max(pos.lon_deg),
                ),
            });
            // An absent observed time sorts below every present one, so a timestamped
            // observation always wins over an untimed one rather than the last one read
            // winning by accident.
            let at = ps.timestamps.observed_at.unwrap_or(i64::MIN);
            let entry =
                last.entry(ps.platform_id.clone())
                    .or_insert((at, pos.lat_deg, pos.lon_deg));
            if at >= entry.0 {
                *entry = (at, pos.lat_deg, pos.lon_deg);
            }
        }
    }
    FarmView {
        ledger: MarkLedger::of(envelopes),
        last_position_by_platform: last
            .into_iter()
            .map(|(k, (_, lat, lon))| (k, (lat, lon)))
            .collect(),
        bounds,
    }
}

/// The maintenance view: which platforms reported a condition worth attending to.
#[derive(Debug, Clone, PartialEq)]
pub struct MaintenanceView {
    /// The ledger over everything handed in.
    pub ledger: MarkLedger,
    /// Platforms whose battery fell below the attention threshold, with the lowest value seen.
    pub low_battery: BTreeMap<String, f64>,
    /// Platforms whose motor temperature rose above the attention threshold, with the highest
    /// value seen.
    pub high_motor_temp: BTreeMap<String, f64>,
    /// Every distinct degradation reason seen, with how many observations carried it.
    pub reasons: BTreeMap<String, usize>,
}

/// The battery percentage at or below which a platform is worth attending to.
pub const LOW_BATTERY_PCT: f64 = 20.0;
/// The motor temperature in Celsius at or above which a platform is worth attending to.
pub const HIGH_MOTOR_TEMP_C: f64 = 80.0;

/// Which platforms reported a condition worth attending to.
#[must_use]
pub fn maintenance_view(envelopes: &[EvidenceEnvelope]) -> MaintenanceView {
    let mut low_battery: BTreeMap<String, f64> = BTreeMap::new();
    let mut high_motor_temp: BTreeMap<String, f64> = BTreeMap::new();
    let mut reasons: BTreeMap<String, usize> = BTreeMap::new();
    for e in envelopes {
        let id = &state(e).platform_id;
        if let Some(pct) = reading(e, "battery_pct")
            && pct <= LOW_BATTERY_PCT
        {
            let slot = low_battery.entry(id.clone()).or_insert(pct);
            *slot = slot.min(pct);
        }
        if let Some(c) = reading(e, "motor_temp_c")
            && c >= HIGH_MOTOR_TEMP_C
        {
            let slot = high_motor_temp.entry(id.clone()).or_insert(c);
            *slot = slot.max(c);
        }
        if e.claim.mark.status == MarkStatus::Degraded {
            for token in e
                .claim
                .mark
                .reason_code
                .split('|')
                .filter(|t| !t.is_empty())
            {
                *reasons.entry(token.to_string()).or_insert(0) += 1;
            }
        }
    }
    MaintenanceView {
        ledger: MarkLedger::of(envelopes),
        low_battery,
        high_motor_temp,
        reasons,
    }
}

/// The inspection view: what a human should look at, and why.
#[derive(Debug, Clone, PartialEq)]
pub struct InspectionView {
    /// The ledger over everything handed in.
    pub ledger: MarkLedger,
    /// One entry per observation that is not plainly usable, as
    /// `(platform, status, reason_code)`.
    pub follow_up: Vec<(String, MarkStatus, String)>,
}

/// What a human should look at, and why.
///
/// Everything not marked OK is surfaced with its reason. A view that filtered by reason would
/// be deciding on the reader's behalf which degradations matter.
#[must_use]
pub fn inspection_view(envelopes: &[EvidenceEnvelope]) -> InspectionView {
    InspectionView {
        ledger: MarkLedger::of(envelopes),
        follow_up: envelopes
            .iter()
            .filter(|e| e.claim.mark.status != MarkStatus::Ok)
            .map(|e| {
                (
                    state(e).platform_id.clone(),
                    e.claim.mark.status,
                    e.claim.mark.reason_code.clone(),
                )
            })
            .collect(),
    }
}

/// The logistics view: how many platforms are in each domain, and where each one last was.
#[derive(Debug, Clone, PartialEq)]
pub struct LogisticsView {
    /// The ledger over everything handed in.
    pub ledger: MarkLedger,
    /// How many distinct platforms were seen per declared domain.
    ///
    /// `Unknown` is a key like any other. Folding it into one of the three would be inventing
    /// a domain the source declined to declare.
    pub platforms_by_domain: BTreeMap<String, usize>,
    /// Every distinct platform seen, in a stable order.
    pub platforms: Vec<String>,
}

/// How many platforms are in each domain.
#[must_use]
pub fn logistics_view(envelopes: &[EvidenceEnvelope]) -> LogisticsView {
    let mut by_domain: BTreeMap<String, std::collections::BTreeSet<String>> = BTreeMap::new();
    let mut platforms: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for e in envelopes {
        let ps = state(e);
        platforms.insert(ps.platform_id.clone());
        by_domain
            .entry(format!("{:?}", ps.platform_domain))
            .or_default()
            .insert(ps.platform_id.clone());
    }
    LogisticsView {
        ledger: MarkLedger::of(envelopes),
        platforms_by_domain: by_domain.into_iter().map(|(k, v)| (k, v.len())).collect(),
        platforms: platforms.into_iter().collect(),
    }
}

/// The disaster-response view: the accounting ledger, and what it costs to be sure of it.
#[derive(Debug, Clone, PartialEq)]
pub struct DisasterView {
    /// The ledger over everything handed in.
    pub ledger: MarkLedger,
    /// How many envelopes carry a content digest, and are therefore checkable at all.
    pub sealed: usize,
    /// How many envelopes do not, and are therefore not checkable.
    pub unsealed: usize,
    /// How many observations carry no absolute observed time.
    pub without_absolute_time: usize,
}

/// The accounting view: everything that arrived, sorted by how much it can be relied on.
///
/// This is the view that matters when the link has been bad: it does not filter, it counts.
/// A response reading it can see how much of what it has is checkable and how much is not,
/// instead of seeing a clean list that quietly excluded the rest.
#[must_use]
pub fn disaster_view(envelopes: &[EvidenceEnvelope]) -> DisasterView {
    let sealed = envelopes
        .iter()
        .filter(|e| e.content_digest.is_some())
        .count();
    DisasterView {
        ledger: MarkLedger::of(envelopes),
        sealed,
        unsealed: envelopes.len() - sealed,
        without_absolute_time: envelopes
            .iter()
            .filter(|e| state(e).timestamps.observed_at.is_none())
            .count(),
    }
}

/// The industrial view: how much of a run is usable, per platform.
#[derive(Debug, Clone, PartialEq)]
pub struct IndustrialView {
    /// The ledger over everything handed in.
    pub ledger: MarkLedger,
    /// Per platform, `(ok, total)`.
    pub per_platform: BTreeMap<String, (usize, usize)>,
}

impl IndustrialView {
    /// The share of a platform's observations that are plainly usable, in `[0.0, 1.0]`.
    ///
    /// `None` for a platform that was never seen, which is different from a platform whose
    /// every observation was degraded. Returning `0.0` for both would collapse "no data" into
    /// "bad data".
    #[must_use]
    pub fn usable_share(&self, platform: &str) -> Option<f64> {
        self.per_platform.get(platform).map(|(ok, total)| {
            debug_assert!(*total > 0, "a platform is only recorded when it is seen");
            #[allow(clippy::cast_precision_loss)]
            let share = *ok as f64 / *total as f64;
            share
        })
    }
}

/// How much of a run is usable, per platform.
#[must_use]
pub fn industrial_view(envelopes: &[EvidenceEnvelope]) -> IndustrialView {
    let mut per: BTreeMap<String, (usize, usize)> = BTreeMap::new();
    for e in envelopes {
        let entry = per.entry(state(e).platform_id.clone()).or_insert((0, 0));
        entry.1 += 1;
        if e.claim.mark.status == MarkStatus::Ok {
            entry.0 += 1;
        }
    }
    IndustrialView {
        ledger: MarkLedger::of(envelopes),
        per_platform: per,
    }
}
