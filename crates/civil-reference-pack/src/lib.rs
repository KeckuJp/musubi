//! # musubi-civil-reference-pack — a synthetic civil reference pack
//!
//! Seven sources and six consumer views, all synthetic, so that the kernel can be evaluated
//! end to end from public artifacts alone: generate observations, normalize them into sealed
//! evidence, and answer a civil question from the result.
//!
//! ```
//! use musubi_civil_reference_pack::{pack, consumers};
//!
//! let p = pack::ReferencePack::default();
//! let evidence = p.evidence();
//! let farm = consumers::farm_view(&evidence);
//! assert_eq!(farm.ledger.total(), evidence.len(), "every envelope is accounted for");
//! ```
//!
//! ## Provenance: what this pack is made of
//!
//! **Every value is computed from an integer seed by [`synth`].** There is no data file in
//! this crate, nothing is loaded at run time, and no value here was recorded from any
//! deployment, device or site. That matters because "synthetic" is often used for a recorded
//! capture with its identifying fields replaced -- which still carries the timing, cadence and
//! channel correlations of wherever it came from. This pack is not that. A reader who wants to
//! check can read [`synth::SplitMix64`], read the generators in [`sources`], and reproduce
//! every byte.
//!
//! It follows that no coordinate here is anywhere in particular: positions are drawn from a
//! rectangle written down in [`synth::FIELD_LAT`] and [`synth::FIELD_LON`] because they have
//! to be drawn from somewhere.
//!
//! ## The sources
//!
//! An agricultural aircraft and ground vehicle; a camera and a multispectral head carried by
//! the aircraft; an RTK base and a weather station on the ground; and the carrier's own health
//! telemetry. See [`sources::SourceKind`], and read the module documentation there for the
//! limitation the current COM imposes on how the sensors are represented -- it is stated
//! rather than worked around.
//!
//! ## The consumers
//!
//! Farm, maintenance, inspection, logistics, disaster response and industrial. Each is a pure
//! function from a slice of envelopes to a summary, and each carries a
//! [`consumers::MarkLedger`] accounting for every envelope it was handed, so "nothing was
//! dropped" is arithmetic rather than an assurance.
//!
//! ## What this pack does not do
//!
//! It reads. It has no path back to any device: nothing here constructs a command, a
//! heartbeat or an acknowledgement, and the consumer views return summaries rather than
//! actions. It also claims no compatibility with any real product, platform or device: the
//! sources are fictional and the wire is this crate's own.

pub mod consumers;
pub mod pack;
pub mod sources;
pub mod synth;

pub use pack::ReferencePack;
pub use sources::{RejectedObservation, SourceKind, SynthObservation};

#[cfg(test)]
mod pack_tests;
