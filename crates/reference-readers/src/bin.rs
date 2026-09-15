use musubi_reference_types::{ChannelId, ClockBasis, FamilyProfile};

use crate::{FieldValue, Observation, ProfileReader, ReadError, observation};

#[derive(Debug, Clone, Copy, Default)]
pub struct ArduPilotBinReader;

#[derive(Debug)]
pub struct UntimedRecord {
    pub offset: usize,
    pub fields: Vec<(String, FieldValue)>,
    pub bytes: Vec<u8>,
}

#[derive(Debug)]
pub struct ReadReport {
    pub observations: Vec<Observation>,
    pub untimed_records: Vec<UntimedRecord>,
    pub data_records: usize,
}

const HEAD: [u8; 2] = [0xA3, 0x95];
const FMT_MSGID: u8 = 128;
const FMT_LEN: usize = 89;

#[derive(Debug, Clone)]
struct Fmt {
    name: String,
    len: usize,
    format: Vec<u8>,
    labels: Vec<String>,
}

fn cstr(b: &[u8]) -> String {
    let end = b.iter().position(|&c| c == 0).unwrap_or(b.len());
    String::from_utf8_lossy(&b[..end]).into_owned()
}

const fn char_len(c: u8) -> Option<usize> {
    Some(match c {
        b'a' => 64,
        b'b' | b'B' | b'M' => 1,
        b'h' | b'H' | b'c' | b'C' => 2,
        b'i' | b'I' | b'f' | b'e' | b'E' | b'L' => 4,
        b'd' | b'q' | b'Q' => 8,
        b'n' => 4,
        b'N' => 16,
        b'Z' => 64,
        _ => return None,
    })
}

fn decode_value(c: u8, b: &[u8]) -> FieldValue {
    match c {
        b'b' => FieldValue::I64(i64::from(b[0] as i8)),
        b'B' | b'M' => FieldValue::I64(i64::from(b[0])),
        b'h' | b'c' => FieldValue::I64(i64::from(i16::from_le_bytes([b[0], b[1]]))),
        b'H' | b'C' => FieldValue::I64(i64::from(u16::from_le_bytes([b[0], b[1]]))),
        b'i' | b'e' | b'L' => {
            FieldValue::I64(i64::from(i32::from_le_bytes([b[0], b[1], b[2], b[3]])))
        }
        b'I' | b'E' => FieldValue::I64(i64::from(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))),
        b'f' => FieldValue::float32_bits(b[..4].try_into().expect("checked f width")),
        b'd' => FieldValue::float64_bits(b[..8].try_into().expect("checked d width")),
        b'q' => FieldValue::I64(i64::from_le_bytes(b[..8].try_into().unwrap_or([0; 8]))),
        b'Q' => FieldValue::I64(u64::from_le_bytes(b[..8].try_into().unwrap_or([0; 8])) as i64),
        b'n' | b'N' | b'Z' => FieldValue::Text(cstr(b)),
        b'a' => FieldValue::Text(format!("int16[{}]", b.len() / 2)),
        _ => FieldValue::Blank,
    }
}

#[must_use]
pub fn channel_for(name: &str) -> ChannelId {
    match name {
        "GPS" | "GPA" | "GPS2" | "UBX1" | "UBX2" | "XKF1" | "XKF2" | "XKF3" | "XKF4" | "XKF5"
        | "XKFS" | "NKF1" | "NKF2" | "NKF3" | "NKF4" | "NKF5" => ChannelId::GpsEkf,
        "RCIN" | "RCI2" => ChannelId::Rc,
        "RSSI" | "RAD" => ChannelId::LinkStats,
        "MODE" | "ERR" | "EV" | "MSG" | "ARM" | "BAT" | "POWR" | "PM" | "MAV" => ChannelId::Event,
        _ => ChannelId::Onboard,
    }
}

impl ProfileReader for ArduPilotBinReader {
    fn format_id(&self) -> &'static str {
        "ardupilot_dataflash_bin"
    }
    fn read(&self, profile: &FamilyProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        let report = self.read_report(profile, bytes)?;
        if !report.untimed_records.is_empty() {
            return Err(ReadError::UnsupportedFormat {
                format: "DataFlash has untimed records; use read_report to retain them".into(),
            });
        }
        Ok(report.observations)
    }
}

impl ArduPilotBinReader {
    pub fn read_report(
        &self,
        profile: &FamilyProfile,
        bytes: &[u8],
    ) -> Result<ReadReport, ReadError> {
        if profile.format != "ardupilot_dataflash_bin" {
            return Err(ReadError::Profile("expected DataFlash profile".into()));
        }
        let mut fmts: Vec<Option<Fmt>> = vec![None; 256];
        let mut out = Vec::new();
        let mut untimed_records = Vec::new();
        let mut data_records = 0;
        let mut off = 0usize;
        while off < bytes.len() {
            let rest = &bytes[off..];
            if rest.len() < 3 {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: "truncated record head".into(),
                });
            }
            if rest[..2] != HEAD {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: format!("bad head bytes {:02X} {:02X}", rest[0], rest[1]),
                });
            }
            let msgid = rest[2];
            if msgid == FMT_MSGID {
                if rest.len() < FMT_LEN {
                    return Err(ReadError::Malformed {
                        offset: off,
                        what: "truncated FMT".into(),
                    });
                }
                let ty = rest[3];
                let len = usize::from(rest[4]);
                let name = cstr(&rest[5..9]);
                let format = cstr(&rest[9..25]).into_bytes();
                let labels: Vec<String> =
                    cstr(&rest[25..89]).split(',').map(str::to_string).collect();
                if name.is_empty()
                    || !name.is_ascii()
                    || labels.len() != format.len()
                    || labels
                        .iter()
                        .any(|label| label.is_empty() || !label.is_ascii())
                    || labels
                        .iter()
                        .collect::<std::collections::BTreeSet<_>>()
                        .len()
                        != labels.len()
                {
                    return Err(ReadError::Malformed {
                        offset: off,
                        what: "ambiguous FMT name or labels".into(),
                    });
                }
                let declared: Option<usize> = format.iter().map(|&c| char_len(c)).sum();
                match declared {
                    Some(d) if d + 3 == len => {}
                    _ => {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: format!(
                                "FMT {name}: format '{}' does not match length {len}",
                                String::from_utf8_lossy(&format)
                            ),
                        });
                    }
                }
                fmts[usize::from(ty)] = Some(Fmt {
                    name,
                    len,
                    format,
                    labels,
                });
                off += FMT_LEN;
                continue;
            }
            let Some(f) = fmts[usize::from(msgid)].clone() else {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: format!("msgid {msgid} before its FMT"),
                });
            };
            if rest.len() < f.len {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: format!("truncated {} record", f.name),
                });
            }
            let payload = &rest[3..f.len];
            data_records += 1;
            let mut fields: Vec<(String, FieldValue)> = Vec::with_capacity(f.format.len());
            let mut p = 0usize;
            for (i, &c) in f.format.iter().enumerate() {
                let n = char_len(c).unwrap_or(0);
                let label = f.labels.get(i).cloned().unwrap_or_else(|| format!("f{i}"));
                if c == b'a' {
                    for (index, value) in payload[p..p + n].chunks_exact(2).enumerate() {
                        fields.push((
                            format!("{}.{label}[{index}]", f.name),
                            FieldValue::I64(i64::from(i16::from_le_bytes([value[0], value[1]]))),
                        ));
                    }
                    p += n;
                    continue;
                }
                if c == b'Q'
                    && u64::from_le_bytes(payload[p..p + n].try_into().expect("Q width"))
                        > i64::MAX as u64
                {
                    return Err(ReadError::Malformed {
                        offset: off,
                        what: "DataFlash Q exceeds signed common range".into(),
                    });
                }
                if matches!(c, b'n' | b'N' | b'Z') {
                    let text = &payload[p..p + n];
                    let end = text
                        .iter()
                        .position(|&byte| byte == 0)
                        .unwrap_or(text.len());
                    if std::str::from_utf8(&text[..end]).is_err() {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "invalid DataFlash text encoding".into(),
                        });
                    }
                }
                let value = decode_value(c, &payload[p..p + n]);
                fields.push((format!("{}.{label}", f.name), value));
                p += n;
            }
            if fields
                .iter()
                .map(|(name, _)| name)
                .collect::<std::collections::BTreeSet<_>>()
                .len()
                != fields.len()
            {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: "expanded DataFlash field collision".into(),
                });
            }
            let Some(time_index) = f.labels.iter().position(|label| label == "TimeUS") else {
                untimed_records.push(UntimedRecord {
                    offset: off,
                    fields,
                    bytes: rest[..f.len].to_vec(),
                });
                off += f.len;
                continue;
            };
            if f.format[time_index] != b'Q' {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: "TimeUS must be unsigned64 microseconds".into(),
                });
            }
            let time_us = fields
                .iter()
                .find(|(k, _)| k == &format!("{}.TimeUS", f.name))
                .and_then(|(_, v)| match v {
                    FieldValue::I64(i) => Some(*i),
                    _ => None,
                })
                .expect("validated TimeUS scalar");
            let channel = channel_for(&f.name);
            let mut o = observation(profile, time_us / 1000, channel, fields, false);
            o.t_boot_us = Some(time_us.max(0) as u64);
            o.clock_basis = ClockBasis::BootRelative;
            o.wall_ms = None;
            o.time_confidence = crate::default_time_confidence(ClockBasis::BootRelative);
            if f.name == "GPS" {
                let get = |k: &str| {
                    o.fields
                        .iter()
                        .find(|(n, _)| n == k)
                        .and_then(|(_, v)| match v {
                            FieldValue::I64(i) => Some(*i),
                            _ => None,
                        })
                };
                if let (Some(st), Some(gms), Some(gwk)) =
                    (get("GPS.Status"), get("GPS.GMS"), get("GPS.GWk"))
                    && st >= 3
                    && (gwk > 0 || gms > 0)
                {
                    o.anchor_unix_us = Some(gps_to_unix_us(gwk as u16, gms as u32));
                }
            }
            out.push(o);
            off += f.len;
        }
        Ok(ReadReport {
            observations: out,
            untimed_records,
            data_records,
        })
    }
}

#[must_use]
pub fn gps_to_unix_us(gwk: u16, gms: u32) -> i64 {
    const GPS_EPOCH_UNIX_S: i64 = 315_964_800;
    const LEAP_S: i64 = 18;
    let ms = i64::from(gwk) * 7 * 86_400 * 1000 + i64::from(gms) + GPS_EPOCH_UNIX_S * 1000
        - LEAP_S * 1000;
    ms * 1000
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nonfinite_records_keep_neighbors_bits_and_untimed_accounting() {
        let profile = crate::profile::parse_profile(
            include_str!("../../../profiles/public/ardupilot_plane_bin.toml"),
            "public",
        )
        .unwrap();
        let mut bytes = vec![0xA3, 0x95, 128, 129, 23];
        bytes.extend_from_slice(b"TEST");
        let mut fmt = [0; 16];
        fmt[..3].copy_from_slice(b"Qfd");
        bytes.extend_from_slice(&fmt);
        let mut labels = [0; 64];
        labels[..10].copy_from_slice(b"TimeUS,F,D");
        bytes.extend_from_slice(&labels);
        let values = [
            (1.25_f32.to_bits(), (-2.5_f64).to_bits()),
            (0x7fc00025, 0x7ff8000000000025),
            (f32::INFINITY.to_bits(), f64::NEG_INFINITY.to_bits()),
            (f32::NEG_INFINITY.to_bits(), f64::INFINITY.to_bits()),
            ((-3.5_f32).to_bits(), 4.25_f64.to_bits()),
        ];
        for (index, (f, d)) in values.iter().enumerate() {
            bytes.extend_from_slice(&[0xA3, 0x95, 129]);
            bytes.extend_from_slice(&(index as u64 * 1000).to_le_bytes());
            bytes.extend_from_slice(&f.to_le_bytes());
            bytes.extend_from_slice(&d.to_le_bytes());
        }
        let reader = crate::reader_for(&profile.format).unwrap();
        let observations = reader.read(&profile, &bytes).unwrap();
        assert_eq!(observations.len(), 5);
        assert_eq!(observations[0].fields[1].1, FieldValue::F64(1.25));
        assert_eq!(observations[4].fields[2].1, FieldValue::F64(4.25));
        for index in 1..4 {
            assert_eq!(observations[index].t_boot_us, Some(index as u64 * 1000));
            assert_eq!(
                observations[index].fields[1].1,
                FieldValue::Text(format!("nonfinite_float32:0x{:08x}", values[index].0))
            );
            assert_eq!(
                observations[index].fields[2].1,
                FieldValue::Text(format!("nonfinite_float64:0x{:016x}", values[index].1))
            );
            assert_eq!(observations[index].fields[1].1.as_f64(), None);
        }
        assert!(reader.read(&profile, &bytes[..bytes.len() - 1]).is_err());
        let mut untimed = bytes[..89].to_vec();
        untimed[25..35].copy_from_slice(b"OtherQ,F,D");
        untimed.extend_from_slice(&bytes[112..135]);
        let report = ArduPilotBinReader.read_report(&profile, &untimed).unwrap();
        assert_eq!(report.data_records, 1);
        assert!(report.observations.is_empty());
        assert_eq!(report.untimed_records.len(), 1);
        assert_eq!(report.untimed_records[0].bytes, untimed[89..]);
        assert_eq!(
            report.untimed_records[0].fields[1].1,
            FieldValue::Text("nonfinite_float32:0x7fc00025".into())
        );
        assert!(reader.read(&profile, &untimed).is_err());
    }

    #[test]
    fn arrays_and_untimed_records_are_preserved_without_inventing_time() {
        let profile = crate::profile::parse_profile(
            include_str!("../../../profiles/public/ardupilot_plane_bin.toml"),
            "public",
        )
        .unwrap();
        let log = |format: &[u8], labels: &[u8], body: &[u8]| {
            let width: usize = format.iter().map(|&c| char_len(c).unwrap()).sum();
            let mut bytes = vec![0xA3, 0x95, 128, 129, (width + 3) as u8];
            bytes.extend_from_slice(b"TEST");
            let mut fmt = [0; 16];
            fmt[..format.len()].copy_from_slice(format);
            bytes.extend_from_slice(&fmt);
            let mut names = [0; 64];
            names[..labels.len()].copy_from_slice(labels);
            bytes.extend_from_slice(&names);
            bytes.extend_from_slice(&[0xA3, 0x95, 129]);
            bytes.extend_from_slice(body);
            bytes
        };
        let mut body = 0_u64.to_le_bytes().to_vec();
        for index in 0..32_i16 {
            body.extend_from_slice(&(index - 16).to_le_bytes());
        }
        let timed = log(b"Qa", b"TimeUS,Values", &body);
        let report = ArduPilotBinReader.read_report(&profile, &timed).unwrap();
        assert_eq!(report.data_records, 1);
        assert!(report.untimed_records.is_empty());
        assert_eq!(report.observations[0].fields.len(), 33);
        for index in 0..32 {
            assert_eq!(
                report.observations[0].fields[index + 1],
                (
                    format!("TEST.Values[{index}]"),
                    FieldValue::I64(index as i64 - 16)
                )
            );
        }
        assert_eq!(report.observations[0].t_boot_us, Some(0));
        assert_eq!(report.observations[0].clock_basis, ClockBasis::BootRelative);
        let mut distinct = 7_u64.to_le_bytes().to_vec();
        distinct.extend_from_slice(&9000_u64.to_le_bytes());
        let exact = ArduPilotBinReader
            .read_report(&profile, &log(b"QQ", b"Other.TimeUS,TimeUS", &distinct))
            .unwrap();
        assert_eq!(exact.observations[0].t_boot_us, Some(9000));
        assert_eq!(exact.observations[0].fields[0].1, FieldValue::I64(7));
        let mut different_type = 1_f32.to_le_bytes().to_vec();
        different_type.extend_from_slice(&12000_u64.to_le_bytes());
        let exact = ArduPilotBinReader
            .read_report(
                &profile,
                &log(b"fQ", b"Other.TimeUS,TimeUS", &different_type),
            )
            .unwrap();
        assert_eq!(exact.observations[0].t_boot_us, Some(12000));
        let untimed = log(b"B", b"Value", &[7]);
        let report = ArduPilotBinReader.read_report(&profile, &untimed).unwrap();
        assert!(report.observations.is_empty());
        assert_eq!(report.data_records, report.untimed_records.len());
        assert_eq!(report.untimed_records[0].bytes, untimed[89..]);
        assert_eq!(report.untimed_records[0].fields[0].1, FieldValue::I64(7));
        assert!(ArduPilotBinReader.read(&profile, &untimed).is_err());
        let mut collision = body.clone();
        collision.push(1);
        assert!(
            ArduPilotBinReader
                .read_report(
                    &profile,
                    &log(b"QaB", b"TimeUS,Values,Values[0]", &collision)
                )
                .is_err()
        );
        for bad in [
            log(b"Q", b"TimeUS", &u64::MAX.to_le_bytes()),
            log(b"B", b"TimeUS", &[0]),
            log(b"Q", b"", &[0; 8]),
            log(b"f", b"TimeUS", &f32::NAN.to_le_bytes()),
            log(b"n", b"Text", &[0xff, 0, 0, 0]),
            log(b"BB", b"X,X", &[1, 2]),
        ] {
            assert!(ArduPilotBinReader.read_report(&profile, &bad).is_err());
        }
        let mut wrong = profile.clone();
        wrong.format = "px4_ulog".into();
        assert!(ArduPilotBinReader.read_report(&wrong, &timed).is_err());
    }

    #[test]
    fn gps_week_ms_to_unix_matches_pymavlink_rule() {
        assert_eq!(
            gps_to_unix_us(2434, (86_400 + 9 * 3600 + 18) * 1000),
            1_788_166_800_000_000
        );
    }

    #[test]
    fn fmt_length_mismatch_and_truncated_record_are_malformed() {
        let p = crate::profile::parse_profile(
            r#"
profile_id = "b"
version = "1"
family = "ugv"
source_role = "fc"
format = "ardupilot_dataflash_bin"
extensions = ["bin"]
default_clock_basis = "boot_relative"
[fields]
time = "TimeUS"
"#,
            "public",
        )
        .expect("profile");
        let mut fmt = vec![0xA3, 0x95, 128, 129, 10];
        fmt.extend_from_slice(b"XX\0\0");
        let mut f = [0u8; 16];
        f[..2].copy_from_slice(b"QB");
        fmt.extend_from_slice(&f);
        let mut labels = [0u8; 64];
        labels[..9].copy_from_slice(b"TimeUS,Id");
        fmt.extend_from_slice(&labels);
        assert_eq!(fmt.len(), 89);
        assert!(matches!(
            ArduPilotBinReader.read(&p, &fmt),
            Err(ReadError::Malformed { what, .. }) if what.contains("does not match length")
        ));
        fmt[4] = 12;
        let mut bytes = fmt.clone();
        bytes.extend_from_slice(&[0xA3, 0x95, 129, 1, 2, 3]);
        assert!(matches!(
            ArduPilotBinReader.read(&p, &bytes),
            Err(ReadError::Malformed { what, .. }) if what.contains("truncated")
        ));
        let mut ok = fmt;
        ok.extend_from_slice(&[0xA3, 0x95, 129]);
        ok.extend_from_slice(&5_000_000_u64.to_le_bytes());
        ok.push(7);
        let obs = ArduPilotBinReader.read(&p, &ok).expect("reads");
        assert_eq!(obs.len(), 1);
        assert_eq!(obs[0].t_ms, 5_000);
        assert_eq!(obs[0].channel, ChannelId::Onboard);
    }

    #[test]
    fn record_before_fmt_is_malformed() {
        let p = crate::profile::parse_profile(
            r#"
profile_id = "b"
version = "1"
family = "ugv"
source_role = "fc"
format = "ardupilot_dataflash_bin"
extensions = ["bin"]
default_clock_basis = "boot_relative"
[fields]
time = "TimeUS"
"#,
            "public",
        )
        .expect("profile");
        let bytes = [0xA3, 0x95, 129, 0, 0, 0];
        assert!(matches!(
            ArduPilotBinReader.read(&p, &bytes),
            Err(ReadError::Malformed { what, .. }) if what.contains("before its FMT")
        ));
    }
}
