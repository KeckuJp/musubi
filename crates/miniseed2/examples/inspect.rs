//! Inspect a local recorded file; this example never contacts a station.

use std::io::{self, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    let path = args.next().ok_or("usage: inspect recording.mseed")?;
    if args.next().is_some() {
        return Err("usage: inspect recording.mseed".into());
    }
    let bytes = std::fs::read(path)?;
    let records = musubi_miniseed2::parse(&bytes)?;
    let mut output = io::BufWriter::new(io::stdout().lock());
    writeln!(output, "record_index,sample_index,time_unix_us,value_count")?;
    for (record_index, record) in records.iter().enumerate() {
        for (sample_index, value) in record.samples.iter().enumerate() {
            writeln!(
                output,
                "{record_index},{sample_index},{},{value}",
                record.sample_time_unix_us(sample_index)
            )?;
        }
    }
    output.flush()?;
    Ok(())
}
