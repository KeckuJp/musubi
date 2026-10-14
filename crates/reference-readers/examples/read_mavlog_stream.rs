//! Passive stdin example: `PROFILE.toml SOURCE_ID`; flush each framed event before EOF.
use std::io::{Read, Write};
use std::time::{SystemTime, UNIX_EPOCH};

use musubi_reference_readers::{
    FieldValue,
    mavlog_stream::{FrameResult, GpsJsonlStream, StreamCounts, StreamEvent},
    profile::parse_profile_with_identity,
};
use serde_json::{Value, json};

fn counts_json(c: &StreamCounts) -> Value {
    json!({"bytes_received":c.bytes_received,"frames":c.frames,"observations":c.observations,
        "unsupported_frames":c.unsupported_frames,"invalid_frames":c.invalid_frames,
        "oversized_frames":c.oversized_frames,"incomplete_frames":c.incomplete_frames})
}

fn event_json(event: &StreamEvent) -> Value {
    let (kind, reason, observation) = match &event.result {
        FrameResult::Observation(o) => {
            let fields: serde_json::Map<String, Value> = o
                .fields
                .iter()
                .map(|(key, value)| {
                    let value = match value {
                        FieldValue::I64(n) => json!(n),
                        FieldValue::F64(n) => json!(n),
                        FieldValue::Text(s) => json!(s),
                        FieldValue::Blank => Value::Null,
                    };
                    (key.clone(), value)
                })
                .collect();
            (
                "observation",
                None,
                json!({"t_ms":o.t_ms,"t_boot_us":o.t_boot_us,
                "clock_basis":format!("{:?}",o.clock_basis),"time_confidence":o.time_confidence,
                "wall_ms":o.wall_ms,"anchor_unix_us":o.anchor_unix_us,
                "source_role":format!("{:?}",o.source_role),"channel":o.channel.as_str(),
                "fields":fields,"stale":o.stale,"digest":format!("{:?}",o.digest)}),
            )
        }
        FrameResult::Unsupported(reason) => ("unsupported", Some(*reason), Value::Null),
        FrameResult::Invalid(reason) => ("invalid", Some(*reason), Value::Null),
        FrameResult::Oversized => (
            "oversized",
            Some("frame exceeds 65536 bytes; source bytes are a prefix"),
            Value::Null,
        ),
        FrameResult::IncompleteEof => (
            "incomplete_eof",
            Some("EOF before LF frame boundary"),
            Value::Null,
        ),
    };
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut source_hex = String::with_capacity(event.source_bytes.len() * 2);
    for byte in &event.source_bytes {
        source_hex.push(char::from(HEX[usize::from(byte >> 4)]));
        source_hex.push(char::from(HEX[usize::from(byte & 15)]));
    }
    json!({"kind":kind,"reason":reason,"frame_index":event.frame_index,
        "source_id":event.source_id,"platform_domain":format!("{:?}",event.platform_domain),
        "arrival_unix_ms":event.arrival_unix_ms,"arrival_clock_basis":"untrusted_host_capture",
        "source_bytes_hex":source_hex,"source_bytes_truncated":event.source_bytes_truncated,
        "frame_bytes":event.frame_bytes,"terminated_by_lf":event.terminated_by_lf,
        "observation":observation,"counts":counts_json(&event.counts)})
}

fn write_packet(out: &mut impl Write, packet: &Value) -> std::io::Result<()> {
    serde_json::to_writer(&mut *out, packet).map_err(std::io::Error::other)?;
    out.write_all(b"\n")?;
    out.flush()
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 3 {
        return Err("usage: read_mavlog_stream PROFILE.toml SOURCE_ID < INPUT.jsonl".into());
    }
    let mut profile_source = String::new();
    std::fs::File::open(&args[1])?
        .take(65_537)
        .read_to_string(&mut profile_source)?;
    if profile_source.len() > 65_536 {
        return Err("profile exceeds 65536 bytes".into());
    }
    let (profile, identity) = parse_profile_with_identity(&profile_source, "development")?;
    let units = profile.field_units.clone();
    let mut stream = GpsJsonlStream::new(profile, args[2].clone())?;
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    let mut chunk = [0_u8; 8192];
    let mut delivered = 0_u64;
    loop {
        let n = match input.read(&mut chunk) {
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            other => other?,
        };
        let arrival = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .ok()
            .and_then(|d| i64::try_from(d.as_millis()).ok());
        let mut output_error = None;
        let emit = |event| {
            if output_error.is_some() {
                return;
            }
            let mut packet = event_json(&event);
            packet["profile_identity"] = json!(identity);
            packet["profile_units"] = json!(units);
            packet["identity_basis"] =
                json!("declared source label and loaded profile bytes; not authenticity");
            match write_packet(&mut output, &packet) {
                Ok(()) => delivered += 1,
                Err(error) => output_error = Some(error),
            }
        };
        if n == 0 {
            stream.finish(emit)?;
        } else {
            stream.push(&chunk[..n], arrival, emit)?;
        }
        if let Some(error) = output_error {
            return Err(format!(
                "output failed after {delivered} flushed events; {} frames consumed: {error}",
                stream.counts().frames
            )
            .into());
        }
        if n == 0 {
            break;
        }
    }
    drop(input);
    write_packet(
        &mut output,
        &json!({"kind":"summary","source_id":args[2],
        "profile_identity":identity,"counts":counts_json(stream.counts()),"completed":true}),
    )?;
    if stream.counts().frames != stream.counts().observations {
        return Err("one or more stream frames were not accepted; see counted events".into());
    }
    Ok(())
}
