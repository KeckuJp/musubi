use musubi_field_contracts::{RecordV1, decode_mavlink_distance, encode_ndjson};
use std::io::{Read, Write};

fn run() -> Result<(), String> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 4 || args[1].trim().is_empty() {
        return Err("usage: mavlink_range_receipt CAPTURE SOURCE_ID SYSID COMPID".into());
    }
    let sys: u8 = args[2].parse().map_err(|_| "invalid SYSID")?;
    let comp: u8 = args[3].parse().map_err(|_| "invalid COMPID")?;
    let file = std::fs::File::open(&args[0]).map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    file.take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > 16 * 1024 * 1024 {
        return Err("capture exceeds 16 MiB limit".into());
    }
    let (mut received, mut mapped, mut unmapped, mut foreign, mut rejected) = (0, 0, 0, 0, 0);
    let mut offset = 0;
    let mut stdout = std::io::stdout().lock();
    while offset < bytes.len() {
        if bytes.len() - offset < 4 {
            return Err("truncated length prefix".into());
        }
        let len = u32::from_be_bytes([
            bytes[offset],
            bytes[offset + 1],
            bytes[offset + 2],
            bytes[offset + 3],
        ]) as usize;
        offset += 4;
        if len == 0 || len > 65535 || len > bytes.len() - offset {
            return Err("invalid or truncated datagram length".into());
        }
        let frame = &bytes[offset..offset + len];
        offset += len;
        received += 1;
        if frame.len() >= 12 && frame[0] == 0xfd && (frame[5] != sys || frame[6] != comp) {
            foreign += 1;
            continue;
        }
        match decode_mavlink_distance(frame, &args[1]) {
            Ok(Some(observation)) => {
                let row = encode_ndjson(&[RecordV1::ObservationExtension(observation)])
                    .map_err(|e| e.to_string())?;
                stdout.write_all(&row).map_err(|e| e.to_string())?;
                mapped += 1;
            }
            Ok(None) => unmapped += 1,
            Err(error) => {
                rejected += 1;
                eprintln!("datagram={received} error={error}");
            }
        }
    }
    stdout.flush().map_err(|e| e.to_string())?;
    eprintln!(
        "received={received} mapped={mapped} unmapped={unmapped} foreign={foreign} rejected={rejected}"
    );
    if mapped == 0 || rejected > 0 {
        return Err("no accepted range or rejected input; partial records are not a PASS".into());
    }
    Ok(())
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("range receipt: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
