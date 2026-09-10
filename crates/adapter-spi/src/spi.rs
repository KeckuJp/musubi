//! The adapter SPI: the contract an adapter author implements, and nothing else.
//!
//! Three things live here, and all three are device-agnostic:
//!
//! - [`SourceManifest`] — the read-only self-declaration, held as a value so that a test can
//!   assert it. "This adapter sends nothing back" is a claim; a manifest plus a test is
//!   evidence for it.
//! - [`ReadOnlyTap`] — the contract for a passively consumed source. It has no send, connect
//!   or acknowledge method **by construction**, so an implementation cannot acquire one
//!   without changing this trait, which is a reviewable event.
//! - [`QueuedTap`] — a reference implementation over an in-memory queue, for tests and
//!   simulation.
//!
//! Plus two helpers, [`invalid`] and [`quarantine_error`], so that every adapter raises
//! INVALID and reports a quarantine rejection through the same vocabulary instead of
//! inventing one per adapter.
//!
//! Normalisation itself is not declared here: it is [`musubi_core::Normalizer`], and an
//! adapter implements that trait directly. This module deliberately does not wrap it, because
//! a wrapper would be a second place where the normalisation contract is stated.

use musubi_core::RawObservation;
use musubi_core::quarantine::{QUARANTINE_REASON_CODE, QuarantineReject};
use musubi_types::{Mark, MarkStatus};

/// A source's read-only self-declaration.
///
/// A compile-time-fixed invariant rather than a runtime counter: the tap only receives, so it
/// egresses zero bytes, emits no heartbeat and emits no command. Keep these three values as
/// they are when you build your own adapter — they *are* the declaration, and a test that
/// asserts them is what makes the declaration checkable rather than promised in prose.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SourceManifest {
    /// Bytes this adapter sends towards the device. A read-only tap is identically 0.
    pub egress_bytes: u64,
    /// Whether this adapter emits a heartbeat. Read-only implies `false`.
    pub emits_heartbeat: bool,
    /// Whether this adapter emits a command or tasking. Read-only implies `false`.
    pub emits_command: bool,
}

impl SourceManifest {
    /// The read-only declaration, with every egress path closed.
    #[must_use]
    pub const fn read_only() -> Self {
        Self {
            egress_bytes: 0,
            emits_heartbeat: false,
            emits_command: false,
        }
    }

    /// Whether this manifest is the read-only declaration.
    ///
    /// A single predicate so that a conformance test asks one question rather than three,
    /// and so that adding a fourth egress field to this struct forces this function to be
    /// updated rather than leaving a test that silently checks less than it used to.
    #[must_use]
    pub const fn is_read_only(&self) -> bool {
        self.egress_bytes == 0 && !self.emits_heartbeat && !self.emits_command
    }
}

impl Default for SourceManifest {
    fn default() -> Self {
        Self::read_only()
    }
}

/// The contract for a passively consumed source.
///
/// `poll` only pulls the next raw observation; it never injects or sends. A device behind a
/// bidirectional link is still consumed subscribe-only on this side: this trait has no send
/// or command method, so an implementation of it cannot originate one.
pub trait ReadOnlyTap {
    /// This tap's source identifier.
    fn source_id(&self) -> &str;

    /// The read-only invariant declaration.
    fn manifest(&self) -> SourceManifest;

    /// Pull the next raw observation. Never injects and never sends.
    fn poll(&mut self) -> Option<RawObservation>;
}

/// A minimal in-memory read-only tap that hands out frames from a fixed queue.
///
/// In a deployment this is replaced by an MQTT subscribe, a serial read or a UDP receive, all
/// of which are receive-only. `poll` pops the queue, which is a read; it egresses nothing for
/// any input. There is deliberately no `send` or `connect` method on this type.
#[derive(Debug, Clone, Default)]
pub struct QueuedTap {
    source_id: String,
    queue: std::collections::VecDeque<Vec<u8>>,
    received_at: i64,
}

impl QueuedTap {
    /// Build a tap from a source id and a list of already-received frames.
    ///
    /// `received_at` is the epoch-millisecond reception time this tap stamps on every
    /// observation it hands out. A real tap stamps the time it actually received the bytes;
    /// this one takes the value from its caller so that a test stays deterministic. Passing
    /// `0` here is what a normalizer downstream is entitled to treat as "the ingest layer did
    /// not stamp a time", so pass a real one whenever you have it.
    #[must_use]
    pub fn new(source_id: impl Into<String>, frames: Vec<Vec<u8>>, received_at: i64) -> Self {
        Self {
            source_id: source_id.into(),
            queue: frames.into_iter().collect(),
            received_at,
        }
    }
}

impl ReadOnlyTap for QueuedTap {
    fn source_id(&self) -> &str {
        &self.source_id
    }

    fn manifest(&self) -> SourceManifest {
        // Consuming the receive queue only, therefore never sending.
        SourceManifest::read_only()
    }

    fn poll(&mut self) -> Option<RawObservation> {
        // Pop one frame: a read. No injection, no send, no acknowledgement.
        self.queue.pop_front().map(|payload| RawObservation {
            source_id: self.source_id.clone(),
            payload,
            received_at: self.received_at,
        })
    }
}

/// Build an INVALID normalization error, which is the only way an adapter raises INVALID.
///
/// Keep `reason_code` to a closed set of short tokens and put the specific cause in `detail`,
/// so that the cause survives in provenance instead of being flattened into the token. A
/// rejection that carries no detail is a rejection nobody can diagnose.
///
/// Neither argument may contain `';'`: that character is the top-level field separator on the
/// reading side, and a `';'` inside a reason code or a provenance token collides with that
/// boundary. The conformance harness checks this, so a violation surfaces as a test failure
/// rather than as an unsplittable field much later.
#[must_use]
pub fn invalid(
    source_id: &str,
    adapter_slug: &str,
    reason_code: &str,
    detail: impl Into<String>,
) -> musubi_core::NormalizeError {
    musubi_core::NormalizeError {
        source_id: source_id.to_string(),
        mark: Mark {
            status: MarkStatus::Invalid,
            reason_code: reason_code.to_string(),
            provenance: vec![
                format!("source:{source_id}"),
                format!("adapter:{adapter_slug}"),
                detail.into(),
            ],
        },
    }
}

/// Map a quarantine rejection onto a normalization error, using the shared reason-code token.
///
/// Every adapter reports a quarantine rejection with
/// [`musubi_core::quarantine::QUARANTINE_REASON_CODE`], so that the quarantine boundary is
/// visibly one shared mechanism rather than a per-adapter reinvention. A boundary each adapter
/// names differently is a boundary an auditor cannot count.
#[must_use]
pub fn quarantine_error(
    source_id: &str,
    adapter_slug: &str,
    reject: QuarantineReject,
) -> musubi_core::NormalizeError {
    musubi_core::NormalizeError {
        source_id: source_id.to_string(),
        mark: Mark {
            status: MarkStatus::Invalid,
            reason_code: QUARANTINE_REASON_CODE.to_string(),
            provenance: vec![
                format!("source:{source_id}"),
                format!("adapter:{adapter_slug}"),
                format!("quarantine:{reject}"),
            ],
        },
    }
}
