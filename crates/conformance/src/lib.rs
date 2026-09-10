//! Musubi adapter conformance harness.
//!
//! An adapter author runs their [`musubi_core::Normalizer`] against golden and adversarial
//! fixtures and gets back a machine-checked report on the structural invariants of the
//! Common Object Model. The invariant table, the runner and the static write-surface scan all
//! live in [`harness`].
//!
//! The harness does not know about any adapter: [`harness::run_conformance`] takes a
//! `&dyn Normalizer`. That is what lets an adapter author outside this repository check their
//! own crate with it, rather than only the crates that happen to ship alongside it.

pub mod harness;

pub use harness::{
    ConformanceCase, ConformanceReport, ExpectedOutcome, Violation, adopted_count,
    check_no_write_surface, is_adapter_emittable, run_conformance,
};
