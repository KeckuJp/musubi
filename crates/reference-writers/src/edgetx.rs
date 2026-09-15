use crate::{EventKind, FamilyWriter, SourceRole, Timeline, WriteError, events_for};

pub const HEADER: &str = "Date,Time,1RSS(dB),RQly(%),RSNR(dB),TPWR(mW),RxBt(V),Sats,GPS";
pub const HEADER_TMR10MS: &str = "tmr10ms,1RSS(dB),RQly(%),RSNR(dB),TPWR(mW),RxBt(V),Sats,GPS";
pub const TMR10MS_OFFSET: u64 = 3_000;

#[derive(Debug, Clone, Copy, Default)]
pub struct EdgeTxCsvWriter;

impl FamilyWriter for EdgeTxCsvWriter {
    fn format_id(&self) -> &'static str {
        "edgetx_csv"
    }
    fn extension(&self) -> &'static str {
        "csv"
    }
    fn source_role(&self) -> SourceRole {
        SourceRole::Handset
    }
    fn render(&self, timeline: &Timeline, asset_id: &str) -> Result<Vec<u8>, WriteError> {
        let events = events_for(timeline, asset_id, SourceRole::Handset);
        if events.is_empty() {
            return Err(WriteError::NoEvents {
                asset_id: asset_id.to_string(),
                source: SourceRole::Handset,
            });
        }
        let tmr10ms = timeline.asset(asset_id).is_some_and(|a| a.handset_tmr10ms);
        let mut s = String::new();
        s.push_str(if tmr10ms { HEADER_TMR10MS } else { HEADER });
        s.push('\n');
        for e in events {
            let EventKind::HandsetRow {
                rss_dbm,
                rqly_pct,
                rsnr_db,
                tpwr_mw,
                rxbt_v,
                sats,
                gps_deg,
                ..
            } = &e.kind
            else {
                continue;
            };
            if tmr10ms {
                s.push_str(&format!("{},", e.t_ms / 10 + TMR10MS_OFFSET));
            } else {
                let us = timeline.t0_unix_us + (e.t_ms as i64) * 1000;
                let (date, time) = format_date_time(us);
                s.push_str(&format!("{date},{time},"));
            }
            let gps = gps_deg.map_or(String::new(), |(la, lo)| format!("{la:.6} {lo:.6}"));
            s.push_str(&format!(
                "{rss_dbm},{rqly_pct},{rsnr_db},{tpwr_mw},{rxbt_v:.1},{sats},{gps}\n"
            ));
        }
        Ok(s.into_bytes())
    }
}

#[must_use]
pub fn format_date_time(unix_us: i64) -> (String, String) {
    let secs = unix_us.div_euclid(1_000_000);
    let sub_us = unix_us.rem_euclid(1_000_000);
    let cs = sub_us / 10_000; // 10 ms tick
    let days = secs.div_euclid(86_400);
    let sod = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    (
        format!("{y:04}-{m:02}-{d:02}"),
        format!(
            "{:02}:{:02}:{:02}.{cs:02}0",
            sod / 3600,
            (sod % 3600) / 60,
            sod % 60
        ),
    )
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_scenario::{generate, pre_demo_default};

    #[test]
    fn date_time_formatting_matches_edgetx_layout() {
        let (d, t) = format_date_time(1_788_166_800_000_000);
        assert_eq!(d, "2026-08-31");
        assert_eq!(t, "09:00:00.000");
        let (d, t) = format_date_time(0);
        assert_eq!((d.as_str(), t.as_str()), ("1970-01-01", "00:00:00.000"));
        let (_, t) = format_date_time(12_345_670);
        assert_eq!(t, "00:00:12.340");
    }

    #[test]
    fn link_loss_rows_are_numeric_zero_with_gps_blank() {
        let tl = generate(&pre_demo_default(3));
        let csv = String::from_utf8(EdgeTxCsvWriter.render(&tl, "fpv-01").expect("handset rows"))
            .expect("utf8");
        let mut lines = csv.lines();
        assert_eq!(lines.next(), Some(HEADER));
        let rows: Vec<&str> = lines.collect();
        let lost = rows[75];
        assert!(lost.ends_with(",0,0,0,0,0.0,0,"), "{lost}");
        let ok = rows[10];
        assert!(!ok.ends_with(','), "{ok}");
        assert!(ok.contains("35.6"));
    }

    #[test]
    fn tmr10ms_variant_has_no_date_time_columns() {
        let mut s = pre_demo_default(3);
        s.assets[2].handset_tmr10ms = true;
        let tl = generate(&s);
        let csv =
            String::from_utf8(EdgeTxCsvWriter.render(&tl, "fpv-01").expect("rows")).expect("utf8");
        let mut lines = csv.lines();
        assert_eq!(lines.next(), Some(HEADER_TMR10MS));
        assert!(
            lines
                .next()
                .expect("row")
                .starts_with(&format!("{TMR10MS_OFFSET},"))
        );
    }
}
