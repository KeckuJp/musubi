use musubi_reference_types::{ChannelId, ClockBasis, FamilyProfile};

use crate::{FieldValue, Observation, ProfileReader, ReadError, observation};

#[derive(Debug, Clone, Copy, Default)]
pub struct BlackboxCsvReader;

impl ProfileReader for BlackboxCsvReader {
    fn format_id(&self) -> &'static str {
        "blackbox_decoded_csv"
    }
    fn read(&self, profile: &FamilyProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        if profile.format != self.format_id() {
            return Err(ReadError::Profile("expected decoded CSV profile".into()));
        }
        if !profile.channels.is_empty() && !profile.channels.contains(&ChannelId::Onboard) {
            return Err(ReadError::Profile(
                "decoded CSV requires onboard to retain all fields".into(),
            ));
        }
        let table = musubi_decoded_csv::parse(bytes, &profile.fields.time).map_err(|error| {
            ReadError::Malformed {
                offset: error.line,
                what: error.reason.into(),
            }
        })?;
        observations_from_table(profile, &table)
    }
}

pub(crate) fn observations_from_table(
    profile: &FamilyProfile,
    table: &musubi_decoded_csv::Table,
) -> Result<Vec<Observation>, ReadError> {
    if !profile.channels.is_empty() && !profile.channels.contains(&ChannelId::Onboard) {
        return Err(ReadError::Profile(
            "decoded CSV requires onboard to retain all fields".into(),
        ));
    }
    let cols: Vec<&str> = table.columns.iter().map(|c| c.name.as_str()).collect();
    let col = |name: &str| cols.iter().position(|c| *c == name);
    let idx = |names: &[String]| -> Vec<(String, usize)> {
        names
            .iter()
            .filter_map(|n| col(n).map(|i| (n.clone(), i)))
            .collect()
    };
    let rc_i = idx(&profile.fields.status);
    let gps_i = idx(&profile.fields.gps);
    let link_i = idx(&profile.fields.link);
    let enabled = |channel| profile.channels.is_empty() || profile.channels.contains(&channel);
    let mut out = Vec::new();
    for row in &table.rows {
        let t_us = row.boot_us;
        let t_ms = (t_us / 1000) as i64;
        let values: Vec<FieldValue> = row
            .values
            .iter()
            .map(|value| match value {
                musubi_decoded_csv::Value::Integer(v) => FieldValue::I64(*v),
                musubi_decoded_csv::Value::Number(v) => FieldValue::F64(*v),
                musubi_decoded_csv::Value::Text(v) => FieldValue::Text(v.clone()),
                musubi_decoded_csv::Value::Blank => FieldValue::Blank,
            })
            .collect();
        let all: Vec<(String, FieldValue)> = cols
            .iter()
            .enumerate()
            .map(|(i, c)| ((*c).to_string(), values[i].clone()))
            .collect();
        let mut o = observation(profile, t_ms, ChannelId::Onboard, all, false);
        o.t_boot_us = Some(t_us);
        out.push(o);
        let pick = |cols: &[(String, usize)]| -> Vec<(String, FieldValue)> {
            cols.iter()
                .map(|(n, i)| (n.clone(), values[*i].clone()))
                .collect()
        };
        if enabled(ChannelId::Rc) && !rc_i.is_empty() {
            let f = pick(&rc_i);
            let rx_lost = f
                .iter()
                .find(|(k, _)| k == "rxSignalReceived")
                .is_some_and(|(_, v)| matches!(v, FieldValue::I64(0)));
            let mut r = observation(profile, t_ms, ChannelId::Rc, f, rx_lost);
            r.t_boot_us = Some(t_us);
            out.push(r);
        }
        if enabled(ChannelId::LinkStats) && !link_i.is_empty() {
            let mut l = observation(profile, t_ms, ChannelId::LinkStats, pick(&link_i), false);
            l.t_boot_us = Some(t_us);
            out.push(l);
        }
        if enabled(ChannelId::GpsEkf) && !gps_i.is_empty() {
            let mut g = observation(profile, t_ms, ChannelId::GpsEkf, pick(&gps_i), false);
            g.t_boot_us = Some(t_us);
            out.push(g);
        }
    }
    for observation in &mut out {
        observation.clock_basis = ClockBasis::BootRelative;
        observation.wall_ms = None;
        observation.time_confidence = crate::default_time_confidence(ClockBasis::BootRelative);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_names_do_not_change_semantics_and_boot_time_is_not_wall_time() {
        let base = r#"
profile_id = "betaflight_blackbox_csv"
version = "1"
family = "fpv"
source_role = "fc"
format = "blackbox_decoded_csv"
extensions = ["csv"]
default_clock_basis = "host_received"
channels = ["onboard", "link_stats"]
[fields]
time = "time"
link = ["customLink"]
"#;
        let a = crate::profile::parse_profile(base, "public").expect("profile");
        let b = crate::profile::parse_profile(
            &base.replace("betaflight_blackbox_csv", "another_version"),
            "public",
        )
        .expect("profile");
        let input = b"time (us),customLink,vendorNew,rssi\n1001,73,hello,99\n";
        let observations = BlackboxCsvReader.read(&a, input).expect("reads");
        assert_eq!(
            observations,
            BlackboxCsvReader.read(&b, input).expect("reads")
        );
        assert_eq!(observations.len(), 2);
        assert_eq!(observations[0].t_boot_us, Some(1001));
        assert_eq!(observations[0].clock_basis, ClockBasis::BootRelative);
        assert_eq!(observations[0].wall_ms, None);
        assert_eq!(
            crate::field(&observations[0], "vendorNew"),
            Some(&FieldValue::Text("hello".into()))
        );
        assert_eq!(
            observations[1].fields,
            vec![("customLink".into(), FieldValue::I64(73))]
        );
    }

    #[test]
    fn unit_suffix_is_stripped_and_rx_loss_is_stale() {
        let p = crate::profile::parse_profile(
            r#"
profile_id = "bb"
version = "1"
family = "fpv"
source_role = "fc"
format = "blackbox_decoded_csv"
extensions = ["csv"]
default_clock_basis = "boot_relative"
[fields]
time = "time"
status = ["rxSignalReceived", "rxFlightChannelsValid", "failsafePhase"]
link = ["rssi"]
gps = ["GPS_numSat", "GPS_coord[0]", "GPS_coord[1]"]
"#,
            "public",
        )
        .expect("profile");
        let csv = b"loopIteration, time (us), rssi, GPS_numSat, failsafePhase (flags), rxSignalReceived (flags)\n128, 8000000, 900, 12, 0, 1\n256, 8100000, 0, 12, 1, 0\n";
        let obs = BlackboxCsvReader.read(&p, csv).expect("reads");
        assert_eq!(obs.len(), 8);
        assert_eq!(obs[0].t_ms, 8_000);
        assert_eq!(obs[0].channel, ChannelId::Onboard);
        let rc2 = obs
            .iter()
            .filter(|o| o.channel == ChannelId::Rc)
            .nth(1)
            .expect("rc");
        assert!(rc2.stale);
    }
}
