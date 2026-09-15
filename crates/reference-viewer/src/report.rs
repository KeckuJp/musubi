use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::Read;
use std::path::Path;

pub const MAX_REPORT_BYTES: usize = 128 * 1024 * 1024;
const MAX_SOURCE_BYTES: u64 = 16 * 1024 * 1024;
#[derive(Debug)]
pub struct Report {
    pub document: Value,
    pub file_sha256: String,
}
fn require(ok: bool, why: &str) -> Result<(), String> {
    if ok {
        Ok(())
    } else {
        Err(format!("Invalid observation report: {why}"))
    }
}
fn member<'a>(v: &'a Value, key: &str) -> Result<&'a Value, String> {
    v.get(key)
        .ok_or_else(|| format!("Invalid observation report: missing {key}"))
}
fn text(v: &Value, key: &str) -> Result<(), String> {
    require(member(v, key)?.is_string(), key)
}
fn declaration(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
}
fn nullable(v: &Value, key: &str, check: fn(&Value) -> bool) -> Result<(), String> {
    let n = member(v, key)?;
    require(n.is_null() || check(n), key)
}
fn hex(s: &str) -> Result<Vec<u8>, String> {
    require(
        s.len().is_multiple_of(2) && s.bytes().all(|b| b.is_ascii_hexdigit()),
        "source hex",
    )?;
    s.as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let n = |b: u8| match b {
                b'0'..=b'9' => b - b'0',
                b'a'..=b'f' => b - b'a' + 10,
                _ => b - b'A' + 10,
            };
            Ok((n(pair[0]) << 4) | n(pair[1]))
        })
        .collect()
}
pub fn parse(bytes: &[u8]) -> Result<Report, String> {
    require(bytes.len() <= MAX_REPORT_BYTES, "report exceeds 128 MiB")?;
    let document = musubi_jsonl_log::parse_unique_json(bytes)
        .map_err(|_| "Invalid observation report: JSON or duplicate key".to_string())?;
    let d = &document;
    require(
        d["schema"] == "recorded-observation-report/v1" && d["com_sealed"] == false,
        "schema or sealed claim",
    )?;
    text(d, "source_id")?;
    require(
        member(d, "received_at_ms")?.as_i64().is_some_and(|n| n > 0),
        "receipt time",
    )?;
    for key in ["input_sha256", "profile_sha256", "meanings_sha256"] {
        let h = member(d, key)?
            .as_str()
            .ok_or_else(|| format!("Invalid observation report: {key}"))?;
        require(
            h.len() == 64 && h.bytes().all(|b| b.is_ascii_hexdigit()),
            "SHA-256 syntax",
        )?;
    }
    let size = member(d, "source_bytes")?
        .as_u64()
        .ok_or("Invalid observation report: source bytes")?;
    require(size <= MAX_SOURCE_BYTES, "source exceeds 16 MiB")?;
    let encoded = member(d, "source_hex")?
        .as_str()
        .ok_or("Invalid observation report: source hex")?;
    require(encoded.len() as u64 == size * 2, "source size mismatch")?;
    let source = hex(encoded)?;
    require(
        format!("{:x}", Sha256::digest(&source))
            .eq_ignore_ascii_case(d["input_sha256"].as_str().unwrap_or("")),
        "embedded source digest mismatch",
    )?;
    nullable(d, "source_record_count", Value::is_u64)?;
    require(member(d, "accounting")?.is_object(), "accounting")?;
    let rows = member(d, "observations")?
        .as_array()
        .ok_or("Invalid observation report: observations")?;
    require(
        rows.len() <= 100_000
            && member(d, "observation_count")?.as_u64() == Some(rows.len() as u64),
        "observation count",
    )?;
    for (index, row) in rows.iter().enumerate() {
        require(
            row["observation_index"].as_u64() == Some(index as u64),
            "observation index",
        )?;
        for key in ["source", "source_role", "channel", "clock_basis"] {
            text(row, key)?;
        }
        require(member(row, "t_ms")?.is_i64(), "event time")?;
        require(
            member(row, "time_confidence")?
                .as_f64()
                .is_some_and(|n| (0.0..=1.0).contains(&n)),
            "time confidence",
        )?;
        nullable(row, "t_boot_us", Value::is_u64)?;
        nullable(row, "wall_ms", Value::is_i64)?;
        nullable(row, "anchor_unix_us", Value::is_i64)?;
        require(
            member(row, "stale")?.is_boolean() && member(row, "subject")?.is_object(),
            "stale or subject",
        )?;
        require(
            row["source"] == d["source_id"],
            "row source differs from report source",
        )?;
        for key in ["kind", "frame", "convention"] {
            require(
                declaration(member(&row["subject"], key)?),
                "subject declaration",
            )?;
        }
        let fields = member(row, "fields")?
            .as_array()
            .ok_or("Invalid observation report: fields")?;
        for f in fields {
            text(f, "name")?;
            text(f, "disposition")?;
            let value = member(f, "value")?;
            require(
                value.is_null() || value.is_number() || value.is_string(),
                "field scalar",
            )?;
            let names = ["meaning", "unit", "basis"];
            require(
                names.iter().all(|k| f[*k].is_null()) || names.iter().all(|k| declaration(&f[*k])),
                "partial or invalid meaning declaration",
            )?;
            for key in ["source_unit", "meaning", "unit", "basis"] {
                nullable(f, key, Value::is_string)?;
            }
        }
    }
    Ok(Report {
        document,
        file_sha256: format!("{:x}", Sha256::digest(bytes)),
    })
}
pub fn load(path: &Path) -> Result<Report, String> {
    let metadata =
        std::fs::symlink_metadata(path).map_err(|_| "Cannot inspect report file".to_string())?;
    require(
        metadata.is_file(),
        "expected a regular file, not a link or directory",
    )?;
    require(
        metadata.len() <= MAX_REPORT_BYTES as u64,
        "report exceeds 128 MiB",
    )?;
    let file = std::fs::File::open(path).map_err(|_| "Cannot open report file".to_string())?;
    require(
        file.metadata()
            .map_err(|_| "Cannot inspect opened report")?
            .is_file(),
        "expected a regular file",
    )?;
    let mut bytes = Vec::new();
    file.take(MAX_REPORT_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Cannot read report file".to_string())?;
    parse(&bytes)
}
