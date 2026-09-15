use serde::Serialize;
use serde_json::{Map, Value};

use crate::{ContractResult, DigestV1, Validate};

fn canonicalize(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(canonicalize).collect()),
        Value::Object(map) => {
            let mut entries: Vec<_> = map.into_iter().collect();
            entries.sort_by(|left, right| left.0.cmp(&right.0));
            let mut sorted = Map::new();
            for (key, value) in entries {
                sorted.insert(key, canonicalize(value));
            }
            Value::Object(sorted)
        }
        scalar => scalar,
    }
}

pub fn canonical_json_bytes<T: Serialize>(value: &T) -> ContractResult<Vec<u8>> {
    let value = serde_json::to_value(value)?;
    Ok(serde_json::to_vec(&canonicalize(value))?)
}

pub fn canonical_sha256<T: Serialize>(value: &T) -> ContractResult<DigestV1> {
    let bytes = canonical_json_bytes(value)?;
    Ok(DigestV1::sha256(&bytes))
}

pub fn canonical_validated_sha256<T: Serialize + Validate>(value: &T) -> ContractResult<DigestV1> {
    value.validate()?;
    canonical_sha256(value)
}
