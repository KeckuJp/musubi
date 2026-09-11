//! Validate converted pose CSV and emit its original bytes to stdout, preserving every field.
//! Usage: `inspect_pose_csv [--allow-equal-time] INPUT.csv`
//! The time column is `record_time` in microseconds. Strictly increasing time is the default.
//! Equal-time mode preserves source order without claiming an order between events.
//! Validation completes before any CSV is emitted; diagnostics go only to stderr.

use std::io::Write;

fn run() -> Result<(), String> {
    let mut args = std::env::args_os().skip(1);
    let first = args
        .next()
        .ok_or("usage: inspect_pose_csv [--allow-equal-time] INPUT.csv")?;
    let allow_equal = first == "--allow-equal-time";
    let path = if allow_equal {
        args.next()
            .ok_or("usage: inspect_pose_csv [--allow-equal-time] INPUT.csv")?
    } else {
        first
    };
    if args.next().is_some() {
        return Err("usage: inspect_pose_csv [--allow-equal-time] INPUT.csv".into());
    }
    let bytes = std::fs::read(path).map_err(|_| "could not read input CSV")?;
    if allow_equal {
        musubi_decoded_csv::parse_non_decreasing(&bytes, "record_time")
    } else {
        musubi_decoded_csv::parse(&bytes, "record_time")
    }
    .map_err(|error| error.to_string())?;
    std::io::stdout()
        .lock()
        .write_all(&bytes)
        .map_err(|_| "could not write validated CSV".into())
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("{message}");
            std::process::ExitCode::FAILURE
        }
    }
}
