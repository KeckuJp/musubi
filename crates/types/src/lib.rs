//! Musubi — the Common Object Model subset and the MARK envelope.
//!
//! Pure data. Nothing in this crate carries a method, so no value defined here can act on
//! anything: a normalizer fills the fields and a reader consumes them. That is a property of
//! the crate rather than a convention, and the conformance harness checks it by scanning for
//! public functions that are not on a reviewed list.
//!
//! - [`com`] the observation types: timestamps, position, platform domain and state, track,
//!   payload feed, health, and the host-state observation and the values it carries.
//! - [`mark`] the MARK envelope: the integrity and quality metadata this node attaches.
//! - [`evidence`] the claim, the object enumeration and the Evidence envelope.

pub mod com;
pub mod evidence;
pub mod mark;

pub use com::{
    Health, HostStateCounter, HostStateHost, HostStateLoad, HostStateMemory,
    HostStateNetworkInterface, HostStateObservation, HostStateOsIdentity, PayloadFeed,
    PlatformDomain, PlatformState, Position, Timestamps, Track,
};
pub use evidence::{Claim, ComObject, ConfidenceBasis, EvidenceEnvelope};
pub use mark::{Mark, MarkStatus};
