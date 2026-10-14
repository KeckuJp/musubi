#![forbid(unsafe_code)]

/// Unverified synthetic candidate. It is an artifact for the HOLD case, not loaded product code.
#[derive(Debug, Clone, PartialEq)]
pub struct NormalizedRecord {
    pub seq: u64,
    pub event_ms: u64,
    pub position_m: f64,
    pub speed_mps: f64,
    pub link_present: bool,
}

/// Decode one `MSYN1` line without I/O, network access, or actuation.
pub fn decode_line(line: &str) -> Result<NormalizedRecord, &'static str> {
    let mut fields = line.trim().split('|');
    if fields.next() != Some("MSYN1") {
        return Err("bad magic");
    }
    let mut seq = None;
    let mut event_ms = None;
    let mut position_mm = None;
    let mut speed_mm_s = None;
    let mut link_present = None;
    for field in fields {
        let (key, value) = field.split_once('=').ok_or("malformed field")?;
        match key {
            "seq" => seq = Some(value.parse().map_err(|_| "invalid seq")?),
            "event_ms" => event_ms = Some(value.parse().map_err(|_| "invalid event time")?),
            "position_mm" => {
                position_mm = Some(value.parse::<f64>().map_err(|_| "invalid position")?)
            }
            "speed_mm_s" => {
                speed_mm_s = Some(value.parse::<f64>().map_err(|_| "invalid speed")?)
            }
            "link" => {
                link_present = Some(match value {
                    "up" => true,
                    "down" => false,
                    _ => return Err("invalid link state"),
                })
            }
            _ => return Err("unknown field"),
        }
    }
    Ok(NormalizedRecord {
        seq: seq.ok_or("missing seq")?,
        event_ms: event_ms.ok_or("missing event time")?,
        position_m: position_mm.ok_or("missing position")? / 1000.0,
        speed_mps: speed_mm_s.ok_or("missing speed")? / 1000.0,
        link_present: link_present.ok_or("missing link state")?,
    })
}
