//! The pack: a fixed set of sources, bound to one seed, that a consumer can be run against.

use crate::sources::{RejectedObservation, SourceKind, SynthObservation, observations, rejections};
use musubi_types::EvidenceEnvelope;

/// The seed the default pack is generated from.
///
/// Fixed so that the default pack is a golden: two readers who build it get the same bytes.
pub const DEFAULT_SEED: u64 = 0x0C17_11_0F_ACE5_u64;

/// How many observations each source emits in the default pack.
pub const DEFAULT_PER_SOURCE: usize = 12;

/// A generated pack: every source's observations, plus the adversarial fixtures.
#[derive(Debug, Clone, PartialEq)]
pub struct ReferencePack {
    /// The seed this pack was generated from.
    pub seed: u64,
    /// How many observations each source emitted.
    pub per_source: usize,
    /// Every observation, grouped by source in [`SourceKind::ALL`] order.
    pub observations: Vec<SynthObservation>,
    /// The fixtures this pack expects to be refused.
    pub rejections: Vec<RejectedObservation>,
}

impl Default for ReferencePack {
    fn default() -> Self {
        Self::new(DEFAULT_SEED, DEFAULT_PER_SOURCE)
    }
}

impl ReferencePack {
    /// Generate a pack.
    ///
    /// Each source is seeded from `seed` mixed with its position in [`SourceKind::ALL`], so
    /// that two sources in one pack do not emit the same stream while the pack as a whole
    /// still depends on one number.
    ///
    /// # Panics
    /// Panics when `per_source` is zero.
    #[must_use]
    pub fn new(seed: u64, per_source: usize) -> Self {
        let observations = SourceKind::ALL
            .iter()
            .enumerate()
            .flat_map(|(i, kind)| {
                observations(
                    *kind,
                    seed ^ (i as u64).wrapping_mul(0x9E37_79B9),
                    per_source,
                )
            })
            .collect();
        Self {
            seed,
            per_source,
            observations,
            rejections: rejections(),
        }
    }

    /// Normalize every observation into sealed evidence.
    #[must_use]
    pub fn evidence(&self) -> Vec<EvidenceEnvelope> {
        self.observations
            .iter()
            .map(SynthObservation::normalize)
            .collect()
    }

    /// The observations from one source.
    #[must_use]
    pub fn from_source(&self, kind: SourceKind) -> Vec<&SynthObservation> {
        self.observations
            .iter()
            .filter(|o| o.kind == kind)
            .collect()
    }

    /// Every wire line this pack emits, in order.
    ///
    /// The bytes an adapter author can point their own decoder at.
    #[must_use]
    pub fn wire_lines(&self) -> Vec<String> {
        self.observations
            .iter()
            .map(SynthObservation::to_wire)
            .collect()
    }
}
