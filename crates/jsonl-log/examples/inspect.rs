//! Inspect a decoded export without invoking a decoder or contacting a device.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 2 {
        return Err("usage: inspect EXPORT.jsonl".into());
    }
    let report = musubi_jsonl_log::parse(&std::fs::read(&args[1])?)?;
    let timed: Vec<_> = report
        .timed_records
        .iter()
        .map(|t| {
            serde_json::json!({
                "record_index":t.record_index,"boot_us":t.boot_us
            })
        })
        .collect();
    serde_json::to_writer(
        std::io::stdout().lock(),
        &serde_json::json!({
            "records":report.records,"timed_records":timed,"untimed_records":report.untimed_records
        }),
    )?;
    Ok(())
}
