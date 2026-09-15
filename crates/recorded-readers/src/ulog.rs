//! Offline ulog decoding and record preservation. See the recorded-input guide.
use crate::config::{ChannelId, ReadProfile};

use crate::{FieldValue, Observation, ProfileReader, ReadError, observation};

#[derive(Debug, Clone, Copy, Default)]
pub struct Px4UlgReader;

#[derive(Debug)]
pub struct UnsupportedRecord {
    pub offset: usize,
    pub reason: &'static str,
    pub bytes: Vec<u8>,
}

#[derive(Debug)]
pub struct ReadReport {
    pub selected_multi_id: u8,
    pub observations: Vec<Observation>,
    pub unsupported_records: Vec<UnsupportedRecord>,
    pub data_records: usize,
    pub decoded_data_records: usize,
}

const MAGIC: [u8; 7] = [0x55, 0x4C, 0x6F, 0x67, 0x01, 0x12, 0x35];

fn text_field(bytes: &[u8], offset: usize) -> Result<&str, ReadError> {
    std::str::from_utf8(bytes).map_err(|_| ReadError::Malformed {
        offset,
        what: "invalid UTF-8 in ULog text; refusing replacement".into(),
    })
}

#[derive(Debug, Clone)]
struct Field {
    ty: String,
    name: String,
    count: usize,
    top_level: bool,
}

fn type_len(ty: &str) -> Option<usize> {
    Some(match ty {
        "int8_t" | "uint8_t" | "bool" | "char" => 1,
        "int16_t" | "uint16_t" => 2,
        "int32_t" | "uint32_t" | "float" => 4,
        "int64_t" | "uint64_t" | "double" => 8,
        _ => return None,
    })
}

fn decode_scalar(ty: &str, b: &[u8]) -> FieldValue {
    match ty {
        "int8_t" => FieldValue::I64(i64::from(b[0] as i8)),
        "uint8_t" | "bool" => FieldValue::I64(i64::from(b[0])),
        "int16_t" => FieldValue::I64(i64::from(i16::from_le_bytes([b[0], b[1]]))),
        "uint16_t" => FieldValue::I64(i64::from(u16::from_le_bytes([b[0], b[1]]))),
        "int32_t" => FieldValue::I64(i64::from(i32::from_le_bytes([b[0], b[1], b[2], b[3]]))),
        "uint32_t" => FieldValue::I64(i64::from(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))),
        "float" => FieldValue::float32_bits(b[..4].try_into().expect("checked scalar length")),
        "int64_t" => FieldValue::I64(i64::from_le_bytes(b[..8].try_into().unwrap_or([0; 8]))),
        "uint64_t" => {
            FieldValue::I64(u64::from_le_bytes(b[..8].try_into().unwrap_or([0; 8])) as i64)
        }
        "double" => FieldValue::float64_bits(b[..8].try_into().expect("checked scalar length")),
        _ => FieldValue::Blank,
    }
}

fn parse_format(def: &str) -> Option<(String, Vec<Field>)> {
    let (name, rest) = def.split_once(':')?;
    let mut fields = Vec::new();
    for item in rest.split(';').filter(|s| !s.is_empty()) {
        let (ty, fname) = item.trim().split_once(' ')?;
        let (ty, count) = match ty.split_once('[') {
            Some((t, n)) => (t.to_string(), n.trim_end_matches(']').parse().ok()?),
            None => (ty.to_string(), 1),
        };
        fields.push(Field {
            ty,
            name: fname.to_string(),
            count,
            top_level: true,
        });
    }
    Some((name.to_string(), fields))
}

fn flatten_fields(fields: &[Field], formats: &[(String, Vec<Field>)]) -> Option<Vec<Field>> {
    fn expand(
        fields: &[Field],
        formats: &[(String, Vec<Field>)],
        prefix: &str,
        depth: usize,
        remaining: &mut usize,
        out: &mut Vec<Field>,
    ) -> Option<()> {
        if depth > 8 {
            return None;
        }
        for field in fields {
            if field.count == 0 || field.count > 4096 {
                return None;
            }
            let name = format!("{prefix}{}", field.name);
            if type_len(&field.ty).is_some() {
                let cost = if field.ty == "char" { 1 } else { field.count };
                *remaining = remaining.checked_sub(cost)?;
                let mut flat = field.clone();
                flat.name = name;
                flat.top_level = depth == 0;
                out.push(flat);
            } else {
                let nested = &formats.iter().find(|(name, _)| name == &field.ty)?.1;
                for index in 0..field.count {
                    let child = if field.count == 1 {
                        format!("{name}.")
                    } else {
                        format!("{name}[{index}].")
                    };
                    expand(nested, formats, &child, depth + 1, remaining, out)?;
                }
            }
        }
        Some(())
    }
    let mut out = Vec::new();
    expand(fields, formats, "", 0, &mut 4096, &mut out)?;
    Some(out)
}

#[must_use]
pub fn channel_for(topic: &str) -> ChannelId {
    match topic {
        "sensor_gps" | "vehicle_gps_position" | "estimator_status" | "estimator_status_flags" => {
            ChannelId::GpsEkf
        }
        "input_rc" => ChannelId::Rc,
        "telemetry_status" => ChannelId::LinkStats,
        "vehicle_status" | "failsafe_flags" | "battery_status" => ChannelId::Event,
        _ => ChannelId::Onboard,
    }
}

impl ProfileReader for Px4UlgReader {
    fn format_id(&self) -> &'static str {
        "px4_ulog"
    }
    fn read(&self, profile: &ReadProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        let report = self.read_report(profile, bytes)?;
        if !report.unsupported_records.is_empty() {
            return Err(ReadError::UnsupportedFormat {
                format:
                    "ULog contains unsupported records; use read_report or a qualified exporter"
                        .into(),
            });
        }
        Ok(report.observations)
    }
}

impl Px4UlgReader {
    pub fn read_report(
        &self,
        profile: &ReadProfile,
        bytes: &[u8],
    ) -> Result<ReadReport, ReadError> {
        self.read_instance_report(profile, bytes, 0)
    }

    pub fn read_instance_report(
        &self,
        profile: &ReadProfile,
        bytes: &[u8],
        selected_multi_id: u8,
    ) -> Result<ReadReport, ReadError> {
        if profile.format != "px4_ulog" {
            return Err(ReadError::Profile("expected px4_ulog profile".into()));
        }
        if bytes.len() < 16 || bytes[..7] != MAGIC {
            return Err(ReadError::Malformed {
                offset: 0,
                what: "not a ULog file (bad magic)".into(),
            });
        }
        if bytes[7] != 1 {
            return Err(ReadError::UnsupportedFormat {
                format: "ULog version other than 1".into(),
            });
        }
        let mut formats: Vec<(String, Vec<Field>)> = Vec::new();
        let mut subs: Vec<Option<(String, Vec<Field>)>> = vec![None; 65_536];
        let mut last_timestamp = vec![None; 65_536];
        let mut out = Vec::new();
        let mut unsupported_records = Vec::new();
        let mut data_records = 0;
        let mut decoded_data_records = 0;
        let mut off = 16usize;
        while off + 3 <= bytes.len() {
            let size = usize::from(u16::from_le_bytes([bytes[off], bytes[off + 1]]));
            let ty = bytes[off + 2];
            let start = off + 3;
            let end = start + size;
            if end > bytes.len() {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: format!("message '{}' exceeds file", ty as char),
                });
            }
            let p = &bytes[start..end];
            match ty {
                b'B' => {
                    if p.len() < 40 {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "short flag-bits message".into(),
                        });
                    }
                    if p[8..16].iter().any(|v| *v != 0) || p[16..40].iter().any(|v| *v != 0) {
                        return Err(ReadError::UnsupportedFormat {
                            format: "ULog incompatibility flags or appended segments".into(),
                        });
                    }
                }
                b'F' => {
                    let def = text_field(p, off)?;
                    if let Some(f) = parse_format(def) {
                        formats.push(f);
                    }
                }
                b'A' => {
                    if p.len() < 3 {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "short A message".into(),
                        });
                    }
                    let multi_id = p[0];
                    let msg_id = usize::from(u16::from_le_bytes([p[1], p[2]]));
                    last_timestamp[msg_id] = None;
                    let name = text_field(&p[3..], off)?.to_owned();
                    if multi_id == selected_multi_id
                        && let Some(f) = formats.iter().find(|(n, _)| *n == name)
                    {
                        subs[msg_id] = Some(f.clone());
                    } else {
                        subs[msg_id] = None;
                    }
                }
                b'R' => {
                    if p.len() != 2 {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "invalid remove-subscription message".into(),
                        });
                    }
                    let msg_id = usize::from(u16::from_le_bytes([p[0], p[1]]));
                    subs[msg_id] = None;
                    last_timestamp[msg_id] = None;
                }
                b'D' => {
                    data_records += 1;
                    if p.len() < 2 {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "short D message".into(),
                        });
                    }
                    let msg_id = usize::from(u16::from_le_bytes([p[0], p[1]]));
                    let Some((topic, definition)) = &subs[msg_id] else {
                        unsupported_records.push(UnsupportedRecord {
                            offset: off,
                            reason: "unresolved or unselected-instance subscription",
                            bytes: bytes[off..end].to_vec(),
                        });
                        off = end;
                        continue; // Unselected instances and unknown subscriptions stay accounted.
                    };
                    let Some(fields) = flatten_fields(definition, &formats) else {
                        unsupported_records.push(UnsupportedRecord {
                            offset: off,
                            reason: "unknown, cyclic or oversized nested definition",
                            bytes: bytes[off..end].to_vec(),
                        });
                        off = end;
                        continue;
                    };
                    let body = &p[2..];
                    let mut vals: Vec<(String, FieldValue)> = Vec::with_capacity(fields.len());
                    let mut pos = 0usize;
                    let mut ok = true;
                    for (field_index, f) in fields.iter().enumerate() {
                        let Some(n) = type_len(&f.ty) else {
                            ok = false;
                            break;
                        };
                        if f.name
                            .rsplit('.')
                            .next()
                            .is_some_and(|name| name.starts_with("_padding"))
                        {
                            if f.top_level && field_index + 1 == fields.len() && pos == body.len() {
                                continue;
                            }
                            let Some(end) = n
                                .checked_mul(f.count)
                                .and_then(|size| pos.checked_add(size))
                                .filter(|end| *end <= body.len())
                            else {
                                return Err(ReadError::Malformed {
                                    offset: off,
                                    what: "truncated ULog padding field".into(),
                                });
                            };
                            pos = end;
                            continue;
                        }
                        if f.ty == "char" {
                            let Some(end) =
                                pos.checked_add(f.count).filter(|end| *end <= body.len())
                            else {
                                return Err(ReadError::Malformed {
                                    offset: off,
                                    what: "truncated ULog char field".into(),
                                });
                            };
                            vals.push((
                                format!("{topic}.{}", f.name),
                                FieldValue::Text(text_field(&body[pos..end], off)?.to_owned()),
                            ));
                            pos += f.count;
                            continue;
                        }
                        for i in 0..f.count {
                            if pos + n > body.len() {
                                return Err(ReadError::Malformed {
                                    offset: off,
                                    what: "truncated ULog scalar field".into(),
                                });
                            }
                            if f.ty == "uint64_t"
                                && u64::from_le_bytes(
                                    body[pos..pos + 8]
                                        .try_into()
                                        .expect("checked scalar length"),
                                ) > i64::MAX as u64
                            {
                                return Err(ReadError::Malformed {
                                    offset: off,
                                    what: "ULog uint64 exceeds common signed integer range".into(),
                                });
                            }
                            if f.ty == "bool" && body[pos] > 1 {
                                return Err(ReadError::Malformed {
                                    offset: off,
                                    what: "ULog bool must be zero or one".into(),
                                });
                            }
                            let key = if f.count == 1 {
                                format!("{topic}.{}", f.name)
                            } else {
                                format!("{topic}.{}[{i}]", f.name)
                            };
                            vals.push((key, decode_scalar(&f.ty, &body[pos..pos + n])));
                            pos += n;
                        }
                    }
                    if !ok {
                        unsupported_records.push(UnsupportedRecord {
                            offset: off,
                            reason: "nested or unknown field type",
                            bytes: bytes[off..end].to_vec(),
                        });
                        off = end;
                        continue; // nested／未知 type は skip（padding 以外は予期しない＝honest skip）。
                    }
                    if pos != body.len()
                        || vals
                            .iter()
                            .map(|(name, _)| name)
                            .collect::<std::collections::BTreeSet<_>>()
                            .len()
                            != vals.len()
                    {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "ULog field collision or trailing data".into(),
                        });
                    }
                    if fields.iter().filter(|f| f.name == "timestamp").count() != 1
                        || !fields
                            .iter()
                            .any(|f| f.name == "timestamp" && f.ty == "uint64_t" && f.count == 1)
                    {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "ULog data requires one uint64 timestamp".into(),
                        });
                    }
                    let t_us = vals
                        .iter()
                        .find(|(k, _)| k == &format!("{topic}.timestamp"))
                        .and_then(|(_, v)| match v {
                            FieldValue::I64(i) => Some(*i),
                            _ => None,
                        })
                        .ok_or_else(|| ReadError::Malformed {
                            offset: off,
                            what: "missing ULog timestamp value".into(),
                        })?;
                    if last_timestamp[msg_id].is_some_and(|previous| t_us < previous) {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "decreasing subscription timestamp".into(),
                        });
                    }
                    last_timestamp[msg_id] = Some(t_us);
                    let get = |k: &str| {
                        vals.iter()
                            .find(|(n, _)| n == &format!("{topic}.{k}"))
                            .and_then(|(_, v)| v.as_f64())
                    };
                    let channel = channel_for(topic);
                    let stale = topic == "input_rc"
                        && (get("rc_lost").is_some_and(|v| v > 0.0)
                            || get("link_quality").is_some_and(|v| v <= 0.0));
                    let anchor = if topic == "sensor_gps" {
                        vals.iter()
                            .find(|(key, _)| key == "sensor_gps.time_utc_usec")
                            .and_then(|(_, value)| match value {
                                FieldValue::I64(v) if *v > 946_684_800_000_000 => Some(*v),
                                _ => None,
                            })
                    } else {
                        None
                    };
                    let mut o = observation(profile, t_us / 1000, channel, vals.clone(), stale);
                    o.t_boot_us = Some(t_us.max(0) as u64);
                    o.anchor_unix_us = anchor;
                    out.push(o);
                    decoded_data_records += 1;
                    if topic == "input_rc" {
                        let link: Vec<(String, FieldValue)> = vals
                            .iter()
                            .filter(|(k, _)| {
                                k.ends_with(".rssi")
                                    || k.ends_with(".rssi_dbm")
                                    || k.ends_with(".link_quality")
                            })
                            .cloned()
                            .collect();
                        let mut l =
                            observation(profile, t_us / 1000, ChannelId::LinkStats, link, stale);
                        l.t_boot_us = Some(t_us.max(0) as u64);
                        out.push(l);
                    }
                }
                b'L' => {
                    if p.len() < 9 {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "short L message".into(),
                        });
                    }
                    let level = p[0];
                    let t_us = u64::from_le_bytes(p[1..9].try_into().unwrap_or([0; 8]));
                    let text = text_field(&p[9..], off)?.to_owned();
                    let fields = vec![
                        ("log.level".to_string(), FieldValue::I64(i64::from(level))),
                        ("log.message".to_string(), FieldValue::Text(text)),
                    ];
                    let mut o = observation(
                        profile,
                        (t_us / 1000) as i64,
                        ChannelId::Event,
                        fields,
                        false,
                    );
                    o.t_boot_us = Some(t_us);
                    out.push(o);
                }
                b'O' => {
                    if p.len() != 2 {
                        return Err(ReadError::Malformed {
                            offset: off,
                            what: "ULog dropout requires exactly two bytes".into(),
                        });
                    }
                    let dur = u16::from_le_bytes([p[0], p[1]]);
                    let last_t = out.last().map_or(0, |o: &Observation| o.t_ms);
                    let fields = vec![(
                        "dropout.duration_ms".to_string(),
                        FieldValue::I64(i64::from(dur)),
                    )];
                    out.push(observation(
                        profile,
                        last_t,
                        ChannelId::Onboard,
                        fields,
                        true,
                    ));
                }
                _ => {}
            }
            off = end;
        }
        if off != bytes.len() {
            return Err(ReadError::Malformed {
                offset: off,
                what: "truncated ULog message header".into(),
            });
        }
        Ok(ReadReport {
            selected_multi_id,
            observations: out,
            unsupported_records,
            data_records,
            decoded_data_records,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nonfinite_bits_boolean_and_dropout_boundaries() {
        let profile = crate::profile::parse_profile(include_str!(
            "../../../profiles/recorded/public/px4_ulg.json"
        ))
        .unwrap();
        let message = |bytes: &mut Vec<u8>, kind: u8, payload: &[u8]| {
            bytes.extend_from_slice(&(payload.len() as u16).to_le_bytes());
            bytes.push(kind);
            bytes.extend_from_slice(payload);
        };
        let base = || {
            let mut bytes = MAGIC.to_vec();
            bytes.push(1);
            bytes.extend_from_slice(&0_u64.to_le_bytes());
            bytes
        };
        for (f, d) in [
            (0x7fc01234_u32, 0xfff0000000000000_u64),
            (0xff800000, 0x7ff8000000001234),
        ] {
            let mut bytes = base();
            message(
                &mut bytes,
                b'F',
                b"sample:uint64_t timestamp;float x;double y;bool valid;",
            );
            message(&mut bytes, b'A', b"\0\0\0sample");
            let mut data = vec![0, 0];
            data.extend_from_slice(&1000_u64.to_le_bytes());
            data.extend_from_slice(&f.to_le_bytes());
            data.extend_from_slice(&d.to_le_bytes());
            data.push(1);
            message(&mut bytes, b'D', &data);
            let report = Px4UlgReader.read_report(&profile, &bytes).unwrap();
            let fields = &report.observations[0].fields;
            assert!(fields.contains(&(
                "sample.x".into(),
                FieldValue::Text(format!("nonfinite_float32:0x{f:08x}"))
            )));
            assert!(fields.contains(&(
                "sample.y".into(),
                FieldValue::Text(format!("nonfinite_float64:0x{d:016x}"))
            )));
            assert!(fields.contains(&("sample.valid".into(), FieldValue::I64(1))));
            *bytes.last_mut().unwrap() = 2;
            assert!(Px4UlgReader.read_report(&profile, &bytes).is_err());
        }
        assert_eq!(
            decode_scalar("float", &1.25_f32.to_le_bytes()),
            FieldValue::F64(1.25)
        );
        assert_eq!(
            decode_scalar("double", &(-2.5_f64).to_le_bytes()),
            FieldValue::F64(-2.5)
        );
        for payload in [vec![], vec![1], vec![1, 0], vec![1, 0, 0]] {
            let mut bytes = base();
            message(&mut bytes, b'O', &payload);
            assert_eq!(
                Px4UlgReader.read_report(&profile, &bytes).is_ok(),
                payload.len() == 2
            );
        }
    }

    #[test]
    fn text_is_exact_utf8_or_rejected_never_replaced() {
        let profile = crate::profile::parse_profile(include_str!(
            "../../../profiles/recorded/public/px4_ulg.json"
        ))
        .unwrap();
        let message = |bytes: &mut Vec<u8>, kind: u8, payload: &[u8]| {
            bytes.extend_from_slice(&(payload.len() as u16).to_le_bytes());
            bytes.push(kind);
            bytes.extend_from_slice(payload);
        };
        let base = || {
            let mut bytes = MAGIC.to_vec();
            bytes.push(1);
            bytes.extend_from_slice(&0_u64.to_le_bytes());
            bytes
        };
        for kind in [b'F', b'A', b'L'] {
            let mut bytes = base();
            let mut payload = vec![
                0;
                if kind == b'L' {
                    9
                } else if kind == b'A' {
                    3
                } else {
                    0
                }
            ];
            payload.push(255);
            message(&mut bytes, kind, &payload);
            assert!(Px4UlgReader.read_report(&profile, &bytes).is_err());
        }
        for text in ["é".as_bytes(), &[255, 0]] {
            let mut bytes = base();
            message(
                &mut bytes,
                b'F',
                b"sample:uint64_t timestamp;char[2] label;",
            );
            message(&mut bytes, b'A', b"\0\0\0sample");
            let mut data = vec![0, 0];
            data.extend_from_slice(&1000_u64.to_le_bytes());
            data.extend_from_slice(text);
            message(&mut bytes, b'D', &data);
            let result = Px4UlgReader.read_report(&profile, &bytes);
            if text[0] == 255 {
                assert!(result.is_err());
            } else {
                assert!(
                    result.unwrap().observations[0]
                        .fields
                        .contains(&("sample.label".into(), FieldValue::Text("é".into()),))
                );
            }
        }
    }

    #[test]
    fn nested_arrays_preserve_paths_padding_and_only_top_level_clock() {
        let profile = crate::profile::parse_profile(include_str!(
            "../../../profiles/recorded/public/px4_ulg.json"
        ))
        .unwrap();
        let message = |bytes: &mut Vec<u8>, kind: u8, payload: &[u8]| {
            bytes.extend_from_slice(&(payload.len() as u16).to_le_bytes());
            bytes.push(kind);
            bytes.extend_from_slice(payload);
        };
        let log = |definition: &[u8], body: &[u8]| {
            let mut bytes = MAGIC.to_vec();
            bytes.push(1);
            bytes.extend_from_slice(&0_u64.to_le_bytes());
            message(
                &mut bytes,
                b'F',
                b"leaf:uint64_t timestamp;int16_t reading;uint8_t[2] _padding0;",
            );
            message(&mut bytes, b'F', definition);
            message(&mut bytes, b'A', b"\0\0\0sample");
            let mut data = vec![0, 0];
            data.extend_from_slice(body);
            message(&mut bytes, b'D', &data);
            bytes
        };
        for count in [1, 2] {
            let definition =
                format!("sample:leaf[{count}] sensors;uint64_t timestamp;uint8_t[2] _padding0;");
            let mut body = Vec::new();
            for index in 0..count {
                body.extend_from_slice(&(9000000_u64 - index as u64).to_le_bytes());
                body.extend_from_slice(&(-12_i16 + index as i16).to_le_bytes());
                body.extend_from_slice(&[0, 0]);
            }
            body.extend_from_slice(&1000_u64.to_le_bytes());
            let rows = Px4UlgReader
                .read(&profile, &log(definition.as_bytes(), &body))
                .unwrap();
            assert_eq!(rows[0].t_boot_us, Some(1000));
            let key = if count == 1 {
                "sample.sensors.reading"
            } else {
                "sample.sensors[0].reading"
            };
            assert!(rows[0].fields.contains(&(key.into(), FieldValue::I64(-12))));
            assert!(
                !rows[0]
                    .fields
                    .iter()
                    .any(|(key, _)| key.contains("_padding"))
            );
            body.push(0); // Partially present outer padding is invalid.
            assert!(
                Px4UlgReader
                    .read(&profile, &log(definition.as_bytes(), &body))
                    .is_err()
            );
        }
        let mut body = 1000_u64.to_le_bytes().to_vec();
        body.extend_from_slice(&2000_u64.to_le_bytes());
        body.extend_from_slice(&1_i16.to_le_bytes());
        assert!(
            Px4UlgReader
                .read(
                    &profile,
                    &log(b"sample:uint64_t timestamp;leaf inner;", &body)
                )
                .is_err()
        );
        for definition in [
            b"sample:uint64_t timestamp;sample cycle;".as_slice(),
            b"sample:uint64_t timestamp;missing unknown;",
            b"sample:uint64_t timestamp;leaf[4097] excessive;",
        ] {
            let bytes = log(definition, &1000_u64.to_le_bytes());
            let report = Px4UlgReader.read_report(&profile, &bytes).unwrap();
            assert_eq!((report.data_records, report.decoded_data_records), (1, 0));
            assert_eq!(report.unsupported_records.len(), 1);
            assert!(Px4UlgReader.read(&profile, &bytes).is_err());
        }
    }

    #[test]
    fn selected_instances_reuse_decoder_without_mixing_values_or_clocks() {
        let profile = crate::profile::parse_profile(include_str!(
            "../../../profiles/recorded/public/px4_ulg.json"
        ))
        .unwrap();
        let message = |bytes: &mut Vec<u8>, kind: u8, payload: &[u8]| {
            bytes.extend_from_slice(&(payload.len() as u16).to_le_bytes());
            bytes.push(kind);
            bytes.extend_from_slice(payload);
        };
        for (first, second) in [(10000_u64, 2000_u64), (32000, 4000)] {
            let mut bytes = MAGIC.to_vec();
            bytes.push(1);
            bytes.extend_from_slice(&0_u64.to_le_bytes());
            message(
                &mut bytes,
                b'F',
                b"sample:uint64_t timestamp;int16_t value;",
            );
            message(&mut bytes, b'A', b"\0\0\0sample");
            message(&mut bytes, b'A', b"\x01\x01\0sample");
            for (id, time, value) in [(0_u16, first, 7_i16), (1, second, -12)] {
                let mut data = id.to_le_bytes().to_vec();
                data.extend_from_slice(&time.to_le_bytes());
                data.extend_from_slice(&value.to_le_bytes());
                message(&mut bytes, b'D', &data);
            }
            for (id, time, value) in [(0, first, 7), (1, second, -12)] {
                let report = Px4UlgReader
                    .read_instance_report(&profile, &bytes, id)
                    .unwrap();
                assert_eq!(report.selected_multi_id, id);
                assert_eq!((report.data_records, report.decoded_data_records), (2, 1));
                assert_eq!(report.unsupported_records.len(), 1);
                assert_eq!(report.observations.len(), 1);
                assert_eq!(report.observations[0].t_boot_us, Some(time));
                assert!(
                    report.observations[0]
                        .fields
                        .contains(&("sample.value".into(), FieldValue::I64(value)))
                );
            }
            let absent = Px4UlgReader
                .read_instance_report(&profile, &bytes, 255)
                .unwrap();
            assert!(absent.observations.is_empty());
            assert_eq!(absent.unsupported_records.len(), 2);
            assert_eq!(absent.decoded_data_records, 0);
            assert!(Px4UlgReader.read(&profile, &bytes).is_err());
        }
    }

    #[test]
    fn clock_and_subscription_contract_is_explicit() {
        let profile = crate::profile::parse_profile(include_str!(
            "../../../profiles/recorded/public/px4_ulg.json"
        ))
        .unwrap();
        let message = |bytes: &mut Vec<u8>, kind: u8, payload: &[u8]| {
            bytes.extend_from_slice(&(payload.len() as u16).to_le_bytes());
            bytes.push(kind);
            bytes.extend_from_slice(payload);
        };
        let header = || {
            let mut bytes = MAGIC.to_vec();
            bytes.push(1);
            bytes.extend_from_slice(&0_u64.to_le_bytes());
            bytes
        };
        let mut bytes = header();
        message(&mut bytes, b'B', &[0; 40]);
        message(
            &mut bytes,
            b'F',
            b"sensor_gps:uint64_t timestamp;uint64_t time_utc_usec;",
        );
        message(&mut bytes, b'A', b"\0\0\0sensor_gps");
        let mut data = vec![0, 0];
        data.extend_from_slice(&1234_u64.to_le_bytes());
        let anchor = 9_007_199_254_740_993_i64;
        data.extend_from_slice(&anchor.to_le_bytes());
        message(&mut bytes, b'D', &data);
        message(&mut bytes, b'D', &data); // Equal timestamps remain two records.
        let report = Px4UlgReader.read_report(&profile, &bytes).unwrap();
        assert_eq!(report.decoded_data_records, 2);
        assert_eq!(report.observations[0].anchor_unix_us, Some(anchor));
        let mut decreasing = bytes.clone();
        data[2..10].copy_from_slice(&1233_u64.to_le_bytes());
        message(&mut decreasing, b'D', &data);
        assert!(Px4UlgReader.read(&profile, &decreasing).is_err());
        message(&mut bytes, b'R', &[0, 0]);
        message(&mut bytes, b'D', &data);
        let report = Px4UlgReader.read_report(&profile, &bytes).unwrap();
        assert_eq!((report.data_records, report.decoded_data_records), (3, 2));
        assert_eq!(report.unsupported_records.len(), 1);
        assert!(Px4UlgReader.read(&profile, &bytes).is_err());
        bytes[7] = 2;
        assert!(Px4UlgReader.read(&profile, &bytes).is_err());
        for flags in [
            vec![0; 39],
            {
                let mut p = vec![0; 40];
                p[8] = 2;
                p
            },
            {
                let mut p = vec![0; 40];
                p[16] = 1;
                p
            },
        ] {
            let mut invalid = header();
            message(&mut invalid, b'B', &flags);
            assert!(Px4UlgReader.read(&profile, &invalid).is_err());
        }
        for format in [
            b"sample:uint64_t other;".as_slice(),
            b"sample:int64_t timestamp;",
        ] {
            let mut invalid = header();
            message(&mut invalid, b'F', format);
            message(&mut invalid, b'A', b"\0\0\0sample");
            message(&mut invalid, b'D', &data[..10]);
            assert!(Px4UlgReader.read(&profile, &invalid).is_err());
        }
    }

    #[test]
    fn scalar_bounds_fail_without_truncation_wrap_or_panic() {
        let profile = crate::profile::parse_profile(
            r#"{"profile_id": "bounds", "version": "1", "family": "fixed_wing", "source_role": "fc", "format": "px4_ulog", "extensions": ["ulg"], "default_clock_basis": "boot_relative", "fields": {"time": "timestamp"}}"#)
        .unwrap();
        let message = |bytes: &mut Vec<u8>, kind: u8, payload: &[u8]| {
            bytes.extend_from_slice(&(payload.len() as u16).to_le_bytes());
            bytes.push(kind);
            bytes.extend_from_slice(payload);
        };
        let log = |format: &[u8], body: &[u8]| {
            let mut bytes = MAGIC.to_vec();
            bytes.push(1);
            bytes.extend_from_slice(&0_u64.to_le_bytes());
            message(&mut bytes, b'F', format);
            message(&mut bytes, b'A', b"\0\0\0sample");
            let mut data = vec![0, 0];
            data.extend_from_slice(body);
            message(&mut bytes, b'D', &data);
            bytes
        };
        let format = b"sample:uint64_t timestamp;char[4] first;char[2] second;";
        let mut body = 1234_u64.to_le_bytes().to_vec();
        body.extend_from_slice(b"abcdef");
        let valid = log(format, &body);
        let observations = Px4UlgReader.read(&profile, &valid).unwrap();
        assert_eq!(observations.len(), 1);
        assert_eq!(observations[0].t_boot_us, Some(1234));
        let unknown = log(b"sample:uint64_t timestamp;unknown nested;", &body);
        let report = Px4UlgReader.read_report(&profile, &unknown).unwrap();
        assert_eq!((report.data_records, report.decoded_data_records), (1, 0));
        assert_eq!(report.unsupported_records.len(), 1);
        let skipped = &report.unsupported_records[0];
        assert_eq!(skipped.bytes, unknown[skipped.offset..]);
        assert!(matches!(
            Px4UlgReader.read(&profile, &unknown),
            Err(ReadError::UnsupportedFormat { .. })
        ));
        let mut mixed = valid.clone();
        message(&mut mixed, b'A', b"\x01\0\0sample");
        let mut data = vec![0, 0];
        data.extend_from_slice(&body);
        message(&mut mixed, b'D', &data);
        let report = Px4UlgReader.read_report(&profile, &mixed).unwrap();
        assert_eq!((report.data_records, report.decoded_data_records), (2, 1));
        assert_eq!(report.observations, observations);
        assert_eq!(report.unsupported_records.len(), 1);
        assert!(Px4UlgReader.read(&profile, &mixed).is_err());
        let padded_format =
            b"sample:uint64_t timestamp;char[4] first;char[2] second;uint8_t[2] _padding0;";
        assert_eq!(
            Px4UlgReader
                .read(&profile, &log(padded_format, &body))
                .unwrap(),
            observations
        );
        let mut padded_body = body.clone();
        padded_body.extend_from_slice(&[0, 0]);
        assert_eq!(
            Px4UlgReader
                .read(&profile, &log(padded_format, &padded_body))
                .unwrap(),
            observations
        );
        padded_body.pop();
        assert!(
            Px4UlgReader
                .read(&profile, &log(padded_format, &padded_body))
                .is_err()
        );
        for length in [7, 9, 12, 13] {
            assert!(
                Px4UlgReader
                    .read(&profile, &log(format, &body[..length]))
                    .is_err()
            );
        }
        body[..8].copy_from_slice(&u64::MAX.to_le_bytes());
        assert!(Px4UlgReader.read(&profile, &log(format, &body)).is_err());
        for tail in [vec![0], vec![0, 0]] {
            let mut truncated = valid.clone();
            truncated.extend(tail);
            assert!(Px4UlgReader.read(&profile, &truncated).is_err());
        }
    }

    #[test]
    fn bad_magic_is_malformed() {
        let p = crate::profile::parse_profile(
            r#"{"profile_id": "u", "version": "1", "family": "fixed_wing", "source_role": "fc", "format": "px4_ulog", "extensions": ["ulg"], "default_clock_basis": "boot_relative", "fields": {"time": "timestamp"}}"#)
        .expect("profile");
        assert!(matches!(
            Px4UlgReader.read(&p, b"ULOG nope........"),
            Err(ReadError::Malformed { .. })
        ));
    }

    #[test]
    fn logged_string_and_dropout_become_event_and_stale_onboard_observations() {
        let p = crate::profile::parse_profile(
            r#"{"profile_id": "u", "version": "1", "family": "fixed_wing", "source_role": "fc", "format": "px4_ulog", "extensions": ["ulg"], "default_clock_basis": "boot_relative", "fields": {"time": "timestamp"}}"#)
        .expect("profile");
        let mut b = MAGIC.to_vec();
        b.push(1);
        b.extend_from_slice(&1_000_000_u64.to_le_bytes());
        let msg = |b: &mut Vec<u8>, ty: u8, p: &[u8]| {
            b.extend_from_slice(&(p.len() as u16).to_le_bytes());
            b.push(ty);
            b.extend_from_slice(p);
        };
        msg(&mut b, b'B', &[0u8; 40]);
        let mut l = vec![b'4'];
        l.extend_from_slice(&2_000_000_u64.to_le_bytes());
        l.extend_from_slice(b"hello");
        msg(&mut b, b'L', &l);
        msg(&mut b, b'O', &150_u16.to_le_bytes());
        let obs = Px4UlgReader.read(&p, &b).expect("reads");
        assert_eq!(obs.len(), 2);
        assert_eq!(obs[0].channel, ChannelId::Event);
        assert_eq!(obs[0].t_ms, 2_000);
        assert!(
            obs[0]
                .fields
                .iter()
                .any(|(k, v)| k == "log.message" && *v == FieldValue::Text("hello".into()))
        );
        assert_eq!(obs[1].channel, ChannelId::Onboard);
        assert!(
            obs[1].stale,
            "dropout is a log-side gap, not a good observation"
        );
        b.extend_from_slice(&[9, 0, b'D', 0]);
        assert!(matches!(
            Px4UlgReader.read(&p, &b),
            Err(ReadError::Malformed { .. })
        ));
    }

    #[test]
    fn format_parser_handles_arrays() {
        let (name, f) =
            parse_format("input_rc:uint64_t timestamp;uint16_t[4] values;bool rc_lost;")
                .expect("parse");
        assert_eq!(name, "input_rc");
        assert_eq!(f.len(), 3);
        assert_eq!((f[1].ty.as_str(), f[1].count), ("uint16_t", 4));
    }
}
