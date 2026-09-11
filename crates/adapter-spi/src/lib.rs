//! # musubi-adapter-spi — the adapter SPI, and one worked adapter that uses it
//!
//! Two halves, deliberately kept apart:
//!
//! - [`spi`] — the contract. A read-only self-declaration you can assert in a test, a tap
//!   trait with no send method, and the two helpers that keep every adapter's INVALID and
//!   quarantine vocabulary identical.
//! - [`template`] — a worked adapter over a synthetic wire. Copy it, rewrite
//!   [`template::parse_my_device`] for your device, and keep the rest.
//!
//! ## What an adapter is, and what it is not
//!
//! An adapter turns bytes a device already sent into a sealed
//! [`musubi_types::EvidenceEnvelope`]. It reads. It has no path back to the device: the tap
//! trait has no send, connect or acknowledge method, the manifest declares zero egress, and a
//! test asserts the manifest. Nothing in this crate originates or forwards an instruction to
//! any device, and nothing in it may be extended to.
//!
//! ## The normalization contract
//!
//! Implement [`musubi_core::Normalizer`]. On success return a **sealed**
//! [`musubi_types::EvidenceEnvelope`] carrying a COM
//! [`musubi_types::PlatformState`]; on failure return a
//! [`musubi_core::NormalizeError`] whose MARK is INVALID. Never return a silent `None` and
//! never drop an observation: a failure that leaves no trace is indistinguishable from an
//! observation that never arrived.
//!
//! ## Dependencies
//!
//! The text template parses its wire by hand. [`recorded_jsonl`] additionally reuses
//! `musubi-jsonl-log` and its JSON value representation. No network or downstream dialect
//! crate is involved; the core dependency closure is unchanged.
//!
//! ## Scope
//!
//! An in-process adapter. Component packaging, a multi-language SDK and a live network tap
//! are not here, and this crate does not claim them.

pub mod recorded_jsonl;
pub mod spi;
pub mod template;

pub use spi::{QueuedTap, ReadOnlyTap, SourceManifest, invalid, quarantine_error};
pub use template::{ADAPTER_SLUG, MyDeviceNormalizer, ParseErr, RawObs, parse_my_device};

#[cfg(test)]
mod spi_tests;

#[cfg(test)]
mod template_tests;
