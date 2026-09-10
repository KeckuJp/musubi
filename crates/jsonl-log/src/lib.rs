//! Offline decoded JSONL log reader. Values are already decoded; no wire scaling is applied.
//! Every record is retained. Time extraction does not establish clock synchronization, platform
//! identity, chronological order or compatibility with a device. No external decoder is invoked.

use serde::{
    Deserialize, Deserializer,
    de::{self, MapAccess, SeqAccess, Visitor},
};
pub use serde_json::Value;

#[derive(Debug, PartialEq, Eq)]
pub struct ParseError {
    pub line: usize,
    pub reason: &'static str,
}
impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "line {}: {}", self.line, self.reason)
    }
}
impl std::error::Error for ParseError {}
fn error(line: usize, reason: &'static str) -> ParseError {
    ParseError { line, reason }
}

#[derive(Debug, PartialEq, Eq)]
pub struct TimedRecord {
    /// Zero-based index into `ReadReport::records`, in source order.
    pub record_index: usize,
    pub boot_us: u64,
}
#[derive(Debug)]
pub struct ReadReport {
    /// All parsed records, including unknown fields and untimed metadata. Not the original bytes.
    pub records: Vec<Value>,
    pub timed_records: Vec<TimedRecord>,
    pub untimed_records: usize,
}

// Ordinary JSON objects silently replace duplicate keys. Reject them at every nesting level
// so field retention remains meaningful; the JSON library still owns grammar and numeric parsing.
struct UniqueValue(Value);
impl<'de> Deserialize<'de> for UniqueValue {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct UniqueVisitor;
        impl<'de> Visitor<'de> for UniqueVisitor {
            type Value = UniqueValue;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("JSON with unique object keys")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Self::Value, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| UniqueValue(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(UniqueValue(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(UniqueValue(v)) = a.next_element()? {
                    values.push(v);
                }
                Ok(UniqueValue(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut values = serde_json::Map::new();
                while let Some((key, UniqueValue(v))) = a.next_entry::<String, UniqueValue>()? {
                    if values.insert(key, v).is_some() {
                        return Err(de::Error::custom("duplicate object key"));
                    }
                }
                Ok(UniqueValue(Value::Object(values)))
            }
        }
        d.deserialize_any(UniqueVisitor)
    }
}

/// Read one JSON object per line with `meta.type` and a `data` object.
/// `TimeUS` and `TimeMS` in data are integer counters, not exporter wall time.
///
/// # Errors
/// Rejects invalid UTF-8/JSON, duplicate keys, wrong shape, invalid/conflicting time counters,
/// and exports with no timed record. Parsing is atomic: errors return no partial report.
pub fn parse(bytes: &[u8]) -> Result<ReadReport, ParseError> {
    let text = std::str::from_utf8(bytes).map_err(|_| error(0, "invalid UTF-8"))?;
    let mut report = ReadReport {
        records: Vec::new(),
        timed_records: Vec::new(),
        untimed_records: 0,
    };
    for (index, line) in text.lines().enumerate() {
        let n = index + 1;
        let UniqueValue(record) = serde_json::from_str(line)
            .map_err(|_| error(n, "invalid JSON or duplicate object key"))?;
        let meta = record
            .get("meta")
            .and_then(Value::as_object)
            .ok_or_else(|| error(n, "missing meta object"))?;
        meta.get("type")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| error(n, "missing message type"))?;
        let data = record
            .get("data")
            .and_then(Value::as_object)
            .ok_or_else(|| error(n, "missing data object"))?;
        let us = data
            .get("TimeUS")
            .map(|v| {
                v.as_u64()
                    .ok_or_else(|| error(n, "TimeUS must be an unsigned integer"))
            })
            .transpose()?;
        let ms = data
            .get("TimeMS")
            .map(|v| {
                v.as_u64()
                    .and_then(|t| t.checked_mul(1000))
                    .ok_or_else(|| error(n, "TimeMS must fit microseconds"))
            })
            .transpose()?;
        if us.is_some() && ms.is_some() && us != ms {
            return Err(error(n, "conflicting time fields"));
        }
        if let Some(boot_us) = us.or(ms) {
            report.timed_records.push(TimedRecord {
                record_index: index,
                boot_us,
            });
        } else {
            report.untimed_records += 1;
        }
        report.records.push(record);
    }
    if report.timed_records.is_empty() {
        return Err(error(0, "no timed records"));
    }
    Ok(report)
}
