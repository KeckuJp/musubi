//! Offline input plus explicit decoding and meaning declarations to common observations.
use musubi_adapter_spi::recorded_observations::{MAX_INPUT_BYTES, MAX_REPORT_BYTES, ingest};
use musubi_decoded_csv::ParseOptions;
use std::io::{Read, Write};
fn read(path: &str, max: usize) -> Result<Vec<u8>, ()> {
    let mut bytes = Vec::new();
    std::fs::File::open(path)
        .map_err(|_| ())?
        .take(max as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ())?;
    if bytes.len() > max {
        return Err(());
    }
    Ok(bytes)
}
fn run() -> Result<(), ()> {
    let a: Vec<_> = std::env::args().collect();
    if a.len() < 6
        || a[6..]
            .iter()
            .any(|s| !["--allow-equal-time", "--preserve-nonfinite-as-text"].contains(&s.as_str()))
    {
        return Err(());
    }
    let bytes = read(&a[1], MAX_INPUT_BYTES)?;
    let profile = String::from_utf8(read(&a[2], 256 * 1024)?).map_err(|_| ())?;
    let meanings = String::from_utf8(read(&a[3], 256 * 1024)?).map_err(|_| ())?;
    let report = ingest(
        &bytes,
        &profile,
        &meanings,
        &a[4],
        a[5].parse().map_err(|_| ())?,
        ParseOptions {
            allow_equal_time: a[6..].iter().any(|s| s == "--allow-equal-time"),
            preserve_nonfinite_as_text: a[6..].iter().any(|s| s == "--preserve-nonfinite-as-text"),
        },
    )
    .map_err(|_| ())?;
    let encoded = serde_json::to_vec(&report.to_json()).map_err(|_| ())?;
    if encoded.len() > MAX_REPORT_BYTES {
        return Err(());
    }
    std::io::stdout()
        .lock()
        .write_all(&encoded)
        .map_err(|_| ())?;
    Ok(())
}
fn main() {
    if run().is_err() {
        eprintln!("{{\"error\":\"recorded observation ingestion failed\"}}");
        std::process::exit(1)
    }
}
