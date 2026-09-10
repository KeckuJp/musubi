//! The Evidence envelope: a normalized observation bound together with the claim this node
//! makes about it.

use crate::{HostStateObservation, Mark, PlatformState};

/// Where a [`Claim`]'s `confidence` came from.
///
/// This separates the value itself from how well the value is grounded. It is the same
/// two-axis idea that surveillance integrity indicators use: one axis for the estimate and
/// a second, separate axis for the quality of its provenance.
///
/// This is **content**: it is included in the canonical bytes and therefore covered by the
/// content digest, so a change to it after sealing is detectable. It is set by the adapter
/// at observation time. It is not one of the reserved, digest-excluded fields on
/// [`EvidenceEnvelope`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfidenceBasis {
    /// A static default the adapter assigned because the upstream wire carried no
    /// confidence at all. **Not a measurement.**
    AdapterAssigned,
    /// Derived from a real signal such as GPS HDOP, filter covariance or SNR.
    Measured {
        /// Reference to the calibration state, if there is one. `None` when uncalibrated.
        calibration_id: Option<String>,
    },
}

/// The claim this node makes about an observation. A claim, not a fact.
#[derive(Debug, Clone, PartialEq)]
pub struct Claim {
    pub claim_id: String,
    pub confidence: f32,
    pub mark: Mark,
    /// Where `confidence` came from. `None` means unstated, which by convention reads as
    /// [`ConfidenceBasis::AdapterAssigned`].
    ///
    /// **Content, digest-included and sealed**: whether two adapters' confidences may be
    /// compared at all turns on this field, so it is covered by tamper detection.
    pub confidence_basis: Option<ConfidenceBasis>,
}

/// The COM object an [`EvidenceEnvelope`] carries.
///
/// A narrow spine on purpose: variants are added when the increment that needs them lands,
/// not in advance. This is an enumeration of data. No variant carries a method, so nothing
/// here can act on anything.
#[derive(Debug, Clone, PartialEq)]
pub enum ComObject {
    /// The state of a platform.
    PlatformState(PlatformState),
    /// A typed host inventory and health observation. It carries no actuation surface.
    HostStateObservation(Box<HostStateObservation>),
}

/// The Evidence envelope: a normalized COM object, the claim about it, and the fields a
/// signature would bind.
///
/// `observation` is the normalized COM body, the thing a reader serializes. `claim` is what
/// this node asserts about that observation: a confidence and a MARK.
///
/// `content_digest` is the SHA-256 of the canonical bytes. It is `None` immediately after
/// normalization and is filled in by the sealing step before fan-out. It is
/// **tamper-evident** — a single flipped bit is detectable — and it is not tamper-proof:
/// without a signature an adversary can recompute the digest to match altered content, so
/// what the digest alone establishes is the detection of non-adversarial corruption such as
/// a bit flip or a transmission error. Signatures and key management are a later increment
/// and are outside this crate.
#[derive(Debug, Clone, PartialEq)]
pub struct EvidenceEnvelope {
    /// The normalized COM object: the body of the observation.
    pub observation: ComObject,
    pub claim: Claim,
    pub classification: Option<String>,
    /// The signature. Unimplemented here and therefore `None`; a trust anchor is a later
    /// increment and lives outside this crate.
    pub signature: Option<Vec<u8>>,
    /// Content fingerprint: the SHA-256 of the canonical bytes.
    ///
    /// `None` immediately after normalization; the sealing step fills it in, once, before
    /// fan-out. It is **excluded** from the canonical bytes it digests, so that it does not
    /// refer to itself.
    pub content_digest: Option<[u8; 32]>,
    /// Signer identity, or per-source-tenant key id. Reserved and always `None` here.
    ///
    /// Paired with `signature`. **Not included in `canonical_bytes`**: whether it is set or
    /// not, the digest of the same content is the same.
    pub signer_id: Option<String>,
    /// Trusted signing time, epoch milliseconds. Reserved; set by the optional signing
    /// feature, `None` here.
    ///
    /// When a trusted timestamp is unreachable, signing time falls back to a self-asserted
    /// clock, and that fallback is reported explicitly as
    /// [`MarkStatus::SelfAssertedTime`](crate::MarkStatus::SelfAssertedTime) rather than
    /// passing silently. Same `i64` epoch-millisecond convention as `received_at`.
    /// **Not included in `canonical_bytes`**.
    pub signed_at: Option<i64>,
    /// A revocation proof captured at signing time, such as a stapled status response or a
    /// freshness token, as opaque bytes. Reserved; set by the optional signing feature.
    ///
    /// A staple that is revoked, or past its next-update while the link is degraded, feeds
    /// [`MarkStatus::StaleRevocation`](crate::MarkStatus::StaleRevocation).
    /// **Not included in `canonical_bytes`**.
    pub revocation_proof: Option<Vec<u8>>,
    /// Opaque diagnostic annotations for an out-of-band provenance view, for example an
    /// observed firmware version or the time of a root-of-trust event. This crate carries
    /// the bytes and does not interpret them; their encoding is decided by whoever writes
    /// them. Always `None` here: no construction site in this tree sets it.
    ///
    /// A reserved field on the same rule as `signer_id`, `signed_at` and
    /// `revocation_proof`: **not included in `canonical_bytes`**, so setting it cannot move
    /// a digest. The deterministic normalizer neither reads nor writes it.
    pub trust_annotations: Option<Vec<u8>>,
}
