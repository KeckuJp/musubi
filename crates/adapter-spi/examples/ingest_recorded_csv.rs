//! Offline direct CSV example. Boot time and position semantics are caller declarations.
use musubi_adapter_spi::{
    recorded_csv::{MAX_REPORT_BYTES, ingest_boot_csv},
    recorded_jsonl::{MAX_INPUT_BYTES, MAX_PROFILE_BYTES, Mapping},
};
use musubi_decoded_csv::ParseOptions;
use std::{
    fs::File,
    io::{Read, Write},
    process::ExitCode,
};

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
    if args.len() < 6 {
        return Err("usage: ingest_recorded_csv INPUT PROFILE.json SOURCE_ID RECEIVED_AT_MS BOOT_TIME_COLUMN [--allow-equal-time] [--preserve-nonfinite-as-text]".into());
    }
    let mut options = ParseOptions::default();
    for option in &args[6..] {
        match option.as_str() {
            "--allow-equal-time" => options.allow_equal_time = true,
            "--preserve-nonfinite-as-text" => options.preserve_nonfinite_as_text = true,
            _ => return Err("unknown-option".into()),
        }
    }
    let profile = read_bounded(&args[2], MAX_PROFILE_BYTES)?;
    let mapping = Mapping::parse(&profile).map_err(|error| error.mark.reason_code)?;
    let received_at = args[4].parse::<i64>().map_err(|_| "invalid-received-at")?;
    let input = read_bounded(&args[1], MAX_INPUT_BYTES)?;
    let report = ingest_boot_csv(&input, &args[5], &mapping, &args[3], received_at, options)
        .map_err(|error| error.mark.reason_code)?;
    let output =
        serde_json::to_vec(&report.to_json()).map_err(|_| "report-serialization-failed")?;
    if output.len() > MAX_REPORT_BYTES {
        return Err("report-oversize".into());
    }
    let stdout = std::io::stdout();
    let mut stream = stdout.lock();
    stream
        .write_all(&output)
        .and_then(|()| stream.write_all(b"\n"))
        .map_err(|_| "report-write-failed")?;
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
