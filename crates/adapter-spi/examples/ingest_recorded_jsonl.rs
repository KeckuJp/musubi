//! Offline CLI. Input and profile reads are bounded before parsing.
use musubi_adapter_spi::recorded_jsonl::{MAX_INPUT_BYTES, MAX_PROFILE_BYTES, Mapping, ingest};
use std::{fs::File, io::Read, process::ExitCode};

fn read_bounded(path: &str, limit: usize) -> Result<Vec<u8>, &'static str> {
    let file = File::open(path).map_err(|_| "input-unreadable")?;
    let mut bytes = Vec::new();
    file.take((limit + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| "input-unreadable")?;
    if bytes.len() > limit {
        return Err("input-oversize");
    }
    Ok(bytes)
}
fn run() -> Result<bool, String> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 5 {
        return Err(
            "usage: ingest_recorded_jsonl INPUT PROFILE.json SOURCE_ID RECEIVED_AT_MS".into(),
        );
    }
    let profile = read_bounded(&args[2], MAX_PROFILE_BYTES)?;
    let mapping = Mapping::parse(&profile).map_err(|e| e.mark.reason_code)?;
    let received_at = args[4].parse::<i64>().map_err(|_| "invalid-received-at")?;
    let input = read_bounded(&args[1], MAX_INPUT_BYTES)?;
    let report = ingest(&input, &mapping, &args[3], received_at).map_err(|e| e.mark.reason_code)?;
    println!("{}", report.to_json());
    Ok(report.rejected.is_empty())
}
fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(reason) => {
            eprintln!("{}", serde_json::json!({"error":reason}));
            ExitCode::FAILURE
        }
    }
}
