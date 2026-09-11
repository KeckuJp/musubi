//! Inspect a local decoded table without inferring clock quality or platform identity.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 3 {
        return Err("usage: inspect INPUT.csv TIME_COLUMN".into());
    }
    let bytes = std::fs::read(&args[1])?;
    let table = musubi_decoded_csv::parse(&bytes, &args[2])?;
    println!(
        "{} rows, {} columns; clock origin and platform not inferred",
        table.rows.len(),
        table.columns.len()
    );
    Ok(())
}
