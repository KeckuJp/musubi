//! The MARK envelope: the integrity and quality metadata this node attaches to an
//! observation.

/// The status carried by a [`Mark`](crate::Mark).
///
/// The deterministic normalizer returns only `Ok`, `Degraded` and `Invalid`. The remaining
/// values are raised on dedicated paths outside it and are listed here so that a reader can
/// tell an adapter-emittable status from a reserved one:
///
/// - `OrderUnknown`: raised by the causal-ordering side channel when two updates are
///   incomparable and no total order may be invented.
/// - `Withheld`: raised by a fan-out when a sink cannot carry the MARK, so the envelope is
///   withheld from that sink rather than being delivered stripped.
/// - `StaleRevocation` and `SelfAssertedTime`: raised by signature verification, which is
///   behind an optional feature and is not part of the deterministic core.
/// - `BufferSaturated`: raised by the bounded store-and-forward buffer when it evicts the
///   oldest undrained envelope instead of aborting.
///
/// The normalizer never returns any of these five.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarkStatus {
    Ok,
    Degraded,
    Invalid,
    OrderUnknown,
    Withheld,
    StaleRevocation,
    /// Signing time fell back to a self-asserted clock because a trusted timestamp was not
    /// reachable. An explicit degradation, not a silent soft-fail. Sibling of
    /// `StaleRevocation`; raised by the optional signature-verification feature, never by
    /// the deterministic normalizer.
    SelfAssertedTime,
    /// A bounded store-and-forward buffer exceeded its capacity and evicted the oldest
    /// undrained envelope, failing open rather than killing the process. Raised by the
    /// caller of the buffer, never by the deterministic normalizer.
    BufferSaturated,
}

/// The MARK envelope: status, reason code and provenance.
///
/// Tamper-evident: a single flipped bit is detectable through the content digest. That is
/// not the same as tamper-proof, and this type does not claim to be.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mark {
    pub status: MarkStatus,
    /// A short account of what is degraded, for example `"no-klv"` or `"time-suspect"`.
    ///
    /// ⚠️ **Wire grammar contract**: when several reasons are composed, the individual
    /// tokens are joined with **`"|"`**, as in `"time-suspect|field-missing:hdg"`.
    /// `";"` is not used, because serializers on the reading side use `";"` as their
    /// top-level field separator; a `";"` inside a reason code collides with that boundary
    /// and makes the field impossible to split again. Individual tokens must not contain
    /// `";"` either.
    pub reason_code: String,
    /// Where the observation came from, appended to as it passes through. Readers join the
    /// entries with `"|"`, on the same rule as `reason_code`.
    pub provenance: Vec<String>,
}
