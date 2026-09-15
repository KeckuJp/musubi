#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]
#![allow(clippy::doc_markdown, clippy::module_name_repetitions)]

mod canonical;
mod contracts;
mod error;
mod mavlink_range;
mod ndjson;
mod registry;

pub use canonical::{canonical_json_bytes, canonical_sha256, canonical_validated_sha256};
pub use contracts::*;
pub use error::{ContractError, ContractResult, Validate};
pub use mavlink_range::decode_mavlink_distance;
pub use ndjson::{RecordV1, decode_ndjson, encode_ndjson};
pub use registry::*;

pub const SCHEMA_VERSION_V1: &str = "1";

pub const SIM_SELF_ATTEST: &str = "SIM_SELF_ATTEST";
