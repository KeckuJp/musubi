use musubi_reference_types::{ChannelId, ClockBasis, FamilyProfile};
use std::collections::BTreeSet;

use crate::{
    FieldValue, Observation, ProfileReader, ReadError, default_time_confidence, observation,
};

#[derive(Debug, Clone, Copy, Default)]
pub struct EdgeTxCsvReader;

fn row_cells(line: &str) -> Option<Vec<&str>> {
    let mut cells = Vec::new();
    let mut rest = line;
    loop {
        let end = if let Some(text) = rest.strip_prefix('"') {
            let end = text.find('"')? + 2;
            if rest.len() > end && rest.as_bytes()[end] != b',' {
                return None;
            }
            end
        } else {
            let end = rest.find(',').unwrap_or(rest.len());
            if rest[..end].contains('"') {
                return None;
            }
            end
        };
        cells.push(&rest[..end]);
        if end == rest.len() {
            return Some(cells);
        }
        rest = &rest[end + 1..];
    }
}

fn parse_value(s: &str) -> FieldValue {
    if let Some(text) = s.strip_prefix('"').and_then(|text| text.strip_suffix('"')) {
        return FieldValue::Text(text.to_string());
    }
    let original = s;
    let s = s.trim();
    if s.is_empty() {
        FieldValue::Blank
    } else if let Ok(i) = s.parse::<i64>() {
        FieldValue::I64(i)
    } else if let Ok(f) = s.parse::<f64>() {
        if f.is_finite()
            && s.trim_start_matches(['-', '+'])
                .bytes()
                .all(|b| b.is_ascii_digit())
        {
            FieldValue::Text(original.to_string())
        } else {
            FieldValue::F64(f)
        }
    } else {
        FieldValue::Text(original.to_string())
    }
}

fn is_zero(v: &FieldValue) -> bool {
    match v {
        FieldValue::I64(i) => *i == 0,
        FieldValue::F64(f) => *f == 0.0,
        FieldValue::Text(_) | FieldValue::Blank => false,
    }
}

fn parse_date_time(date: &str, time: &str) -> Option<i64> {
    if !date.bytes().all(|b| b.is_ascii_digit() || b == b'-')
        || !time
            .bytes()
            .all(|b| b.is_ascii_digit() || b == b':' || b == b'.')
    {
        return None;
    }
    let mut d = date.split('-').map(|x| x.parse::<i64>().ok());
    let (y, m, day) = (d.next()??, d.next()??, d.next()??);
    if d.next().is_some() || !(1..=9999).contains(&y) || !(1..=12).contains(&m) {
        return None;
    }
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let last_day = match m {
        2 => {
            if leap {
                29
            } else {
                28
            }
        }
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    if !(1..=last_day).contains(&day) {
        return None;
    }
    let mut t = time.split(':');
    let h: i64 = t.next()?.parse().ok()?;
    let mi: i64 = t.next()?.parse().ok()?;
    let sec_str = t.next()?;
    if t.next().is_some() || !(0..24).contains(&h) || !(0..60).contains(&mi) {
        return None;
    }
    let (s, frac) = sec_str.split_once('.').unwrap_or((sec_str, "0"));
    let s: i64 = s.parse().ok()?;
    if !(0..60).contains(&s)
        || frac.is_empty()
        || frac.len() > 9
        || !frac.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let ms: i64 = frac
        .chars()
        .chain("000".chars())
        .take(3)
        .collect::<String>()
        .parse()
        .ok()?;
    Some((days_from_civil(y, m, day) * 86_400 + h * 3600 + mi * 60 + s) * 1000 + ms)
}

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

impl ProfileReader for EdgeTxCsvReader {
    fn format_id(&self) -> &'static str {
        "edgetx_csv"
    }
    fn read(&self, profile: &FamilyProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        if profile.format != self.format_id() {
            return Err(ReadError::Profile(
                "handset reader requires its own format".into(),
            ));
        }
        if bytes.len() > 16 * 1024 * 1024 || bytes.contains(&0) {
            return Err(ReadError::Malformed {
                offset: 0,
                what: "unsupported handset CSV representation or size".into(),
            });
        }
        let text = std::str::from_utf8(bytes).map_err(|e| ReadError::Malformed {
            offset: e.valid_up_to(),
            what: "not utf-8".into(),
        })?;
        let mut lines = text.lines().enumerate();
        let (_, header) = lines.next().ok_or_else(|| ReadError::Malformed {
            offset: 0,
            what: "empty csv".into(),
        })?;
        let cols: Vec<&str> = header.split(',').map(str::trim).collect();
        if cols.iter().any(|c| c.is_empty() || c.contains('"'))
            || cols.iter().collect::<BTreeSet<_>>().len() != cols.len()
        {
            return Err(ReadError::Malformed {
                offset: 0,
                what: "duplicate or empty handset column".into(),
            });
        }
        let col = |name: &str| cols.iter().position(|c| *c == name);
        enum TimeCol {
            Rtc(usize, usize),
            Tmr10ms(usize),
        }
        let time_col = match profile.fields.time.as_str() {
            "Date,Time" => match (col("Date"), col("Time")) {
                (Some(date), Some(time)) => TimeCol::Rtc(date, time),
                _ => {
                    return Err(ReadError::Malformed {
                        offset: 0,
                        what: "profile requires Date,Time columns".into(),
                    });
                }
            },
            "tmr10ms" | "Time" => match col(&profile.fields.time) {
                Some(index) => TimeCol::Tmr10ms(index),
                None => {
                    return Err(ReadError::Malformed {
                        offset: 0,
                        what: "profile requires tmr10ms column".into(),
                    });
                }
            },
            "Date,Time|tmr10ms" | "Date,Time|Time|tmr10ms" => {
                match (col("Date"), col("Time"), col("tmr10ms")) {
                    (Some(date), Some(time), None) => TimeCol::Rtc(date, time),
                    (None, Some(time), None) if profile.fields.time == "Date,Time|Time|tmr10ms" => {
                        TimeCol::Tmr10ms(time)
                    }
                    (None, None, Some(index)) => TimeCol::Tmr10ms(index),
                    (Some(_), Some(_), Some(_)) => {
                        return Err(ReadError::Malformed {
                            offset: 0,
                            what: "profile time alternatives are ambiguous: both Date,Time and tmr10ms columns are present".into(),
                        });
                    }
                    _ => {
                        return Err(ReadError::Malformed {
                            offset: 0,
                            what: "profile requires exactly one complete time alternative: Date,Time or tmr10ms".into(),
                        });
                    }
                }
            }
            other => {
                return Err(ReadError::Profile(format!(
                    "unsupported explicit handset clock mapping: {other}"
                )));
            }
        };
        let mapped: Vec<(&str, usize)> = profile
            .fields
            .link
            .iter()
            .chain(&profile.fields.battery)
            .chain(&profile.fields.gps)
            .chain(&profile.fields.status)
            .chain(&profile.fields.health)
            .filter_map(|n| col(n).map(|i| (n.as_str(), i)))
            .collect();
        if mapped.is_empty() {
            return Err(ReadError::Profile(
                "no declared handset sensor found".into(),
            ));
        }
        let link_cols: Vec<usize> = profile.fields.link.iter().filter_map(|n| col(n)).collect();
        let gps_cols: Vec<usize> = profile.fields.gps.iter().filter_map(|n| col(n)).collect();
        let mut out = Vec::new();
        let mut previous = None;
        for (ln, line) in lines {
            let cells = row_cells(line).ok_or_else(|| ReadError::Malformed {
                offset: ln,
                what: "ambiguous handset text quoting".into(),
            })?;
            if cells.len() != cols.len() || line.trim().is_empty() {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "blank or ragged handset row".into(),
                });
            }
            let get = |i: usize| cells.get(i).copied().unwrap_or("");
            if profile
                .fields
                .link
                .iter()
                .chain(&profile.fields.battery)
                .filter_map(|name| col(name))
                .any(|index| get(index).starts_with('"'))
            {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "quoted text cannot replace a selected numeric link/battery value".into(),
                });
            }
            let (t_ms, boot_relative) = match time_col {
                TimeCol::Rtc(di, ti) => (
                    parse_date_time(get(di), get(ti)).ok_or_else(|| ReadError::Malformed {
                        offset: ln,
                        what: format!("bad Date,Time '{},{}'", get(di), get(ti)),
                    })?,
                    false,
                ),
                TimeCol::Tmr10ms(i) => (
                    get(i)
                        .trim()
                        .parse::<u64>()
                        .ok()
                        .and_then(|ticks| ticks.checked_mul(10))
                        .and_then(|ms| i64::try_from(ms).ok())
                        .ok_or_else(|| ReadError::Malformed {
                            offset: ln,
                            what: "bad or out-of-range tmr10ms".into(),
                        })?,
                    true,
                ),
            };
            if previous.is_some_and(|t| t_ms < t) {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "decreasing handset clock; split sessions explicitly".into(),
                });
            }
            previous = Some(t_ms);
            let fields: Vec<(String, FieldValue)> = cols
                .iter()
                .enumerate()
                .map(|(i, n)| ((*n).to_string(), parse_value(get(i))))
                .collect();
            if fields
                .iter()
                .any(|(_, v)| matches!(v, FieldValue::F64(f) if !f.is_finite()))
            {
                return Err(ReadError::Malformed {
                    offset: ln,
                    what: "nonfinite handset value".into(),
                });
            }
            let link_all_zero =
                !link_cols.is_empty() && link_cols.iter().all(|&i| is_zero(&parse_value(get(i))));
            let gps_blank = gps_cols.iter().all(|&i| {
                let v = parse_value(get(i));
                matches!(v, FieldValue::Blank) || is_zero(&v)
            });
            let stale = link_all_zero && gps_blank;
            let mut o = observation(profile, t_ms, ChannelId::LinkStats, fields, stale);
            o.clock_basis = if boot_relative {
                ClockBasis::BootRelative
            } else {
                ClockBasis::Unknown
            };
            o.time_confidence = default_time_confidence(o.clock_basis);
            o.wall_ms = None;
            out.push(o);
        }
        if out.is_empty() {
            return Err(ReadError::Malformed {
                offset: 0,
                what: "no handset records".into(),
            });
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recorded_text_sensor_preserves_commas_and_text_type_through_existing_reader() {
        let p = crate::profile::parse_profile(TMR_PROFILE, "public").unwrap();
        let reader = crate::reader_for(&p.format).unwrap();
        for (text, expected) in [
            ("\"ready,正常\"", "ready,正常"),
            ("\"0012\"", "0012"),
            ("\"\"", ""),
        ] {
            let input = format!("tmr10ms,RQly(%),Text\n3000,100,{text}\n3000,0,{text}\n");
            let output = reader.read(&p, input.as_bytes()).unwrap();
            assert_eq!(output.len(), 2);
            assert_eq!(output[0].t_ms, 30000);
            assert_eq!(output[0].clock_basis, ClockBasis::BootRelative);
            assert_eq!(output[0].fields[2].1, FieldValue::Text(expected.into()));
            assert_eq!(output[0].fields[2].1.as_f64(), None);
            assert!(!output[0].stale);
            assert!(output[1].stale);
        }
        for row in [
            "3000,100,\"unterminated",
            "3000,100,\"a\"tail",
            "3000,100,a\"b",
            "3000,100,\"a\"\"b\"",
            "3000,100,\"two\nlines\"",
            "\"3000\",100,\"text\"",
            "3000,nan,\"text\"",
            "3000,100,\"ok\",extra",
        ] {
            let input = format!("tmr10ms,RQly(%),Text\n{row}\n");
            assert!(reader.read(&p, input.as_bytes()).is_err(), "{row}");
        }
    }

    #[test]
    fn date_time_round_trips_with_writer_epoch() {
        assert_eq!(
            parse_date_time("2026-08-31", "09:00:00.000"),
            Some(1_788_166_800_000)
        );
        assert_eq!(parse_date_time("1970-01-01", "00:00:12.340"), Some(12_340));
        assert_eq!(parse_date_time("2026-8-31", "x"), None);
        assert_eq!(parse_date_time("2026-08-31", "09:00:00.é"), None); // 多バイト小数でも panic しない
    }

    const TMR_PROFILE: &str = r#"
profile_id = "h"
version = "1"
family = "fpv"
source_role = "handset"
format = "edgetx_csv"
extensions = ["csv"]
default_clock_basis = "host_received"
[fields]
time = "tmr10ms"
link = ["RQly(%)"]
"#;

    #[test]
    fn tmr10ms_variant_reads_as_boot_relative_and_missing_time_is_malformed() {
        let p = crate::profile::parse_profile(TMR_PROFILE, "public").expect("profile");
        let obs = EdgeTxCsvReader
            .read(&p, b"tmr10ms,RQly(%)\n3000,100\n3100,0\n")
            .expect("reads");
        assert_eq!(obs.len(), 2);
        assert_eq!(obs[0].t_ms, 30_000);
        assert_eq!(obs[0].clock_basis, ClockBasis::BootRelative);
        assert!(obs[0].wall_ms.is_none());
        assert!(obs[1].stale, "link all zero, no gps column");
        let r = EdgeTxCsvReader.read(&p, b"foo,RQly(%)\n0,100\n");
        assert!(matches!(r, Err(ReadError::Malformed { .. })));
    }

    #[test]
    fn profile_time_mapping_is_not_inert() {
        let incompatible = TMR_PROFILE.replace("tmr10ms", "other_tick");
        let profile = crate::profile::parse_profile(&incompatible, "public").expect("profile");
        let result = EdgeTxCsvReader.read(&profile, b"tmr10ms,RQly(%)\n3000,100\n");
        assert!(matches!(result, Err(ReadError::Profile(_))));
    }

    #[test]
    fn explicit_time_alternative_rejects_ambiguous_partial_and_missing_headers() {
        let composite = TMR_PROFILE.replace("time = \"tmr10ms\"", "time = \"Date,Time|tmr10ms\"");
        let profile = crate::profile::parse_profile(&composite, "public").expect("profile");
        for input in [
            b"Date,Time,tmr10ms,RQly(%)\n2026-08-31,09:00:00.000,3000,100\n".as_slice(),
            b"Date,RQly(%)\n2026-08-31,100\n".as_slice(),
            b"Time,RQly(%)\n09:00:00.000,100\n".as_slice(),
            b"other,RQly(%)\n0,100\n".as_slice(),
        ] {
            assert!(matches!(
                EdgeTxCsvReader.read(&profile, input),
                Err(ReadError::Malformed { .. })
            ));
        }
    }
}
