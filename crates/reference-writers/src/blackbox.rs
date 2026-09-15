use musubi_reference_scenario::FcLogFormat;

use crate::{EventKind, FamilyWriter, SourceRole, Timeline, WriteError, fc_events_for};

pub const HEADER_WITH_RSSI: &str = "loopIteration, time (us), rcCommand[0], rcCommand[1], rcCommand[2], rcCommand[3], vbatLatest (V), amperageLatest (A), rssi, GPS_numSat, GPS_coord[0], GPS_coord[1], failsafePhase (flags), rxSignalReceived (flags), rxFlightChannelsValid (flags)";
pub const HEADER_NO_RSSI: &str = "loopIteration, time (us), rcCommand[0], rcCommand[1], rcCommand[2], rcCommand[3], vbatLatest (V), amperageLatest (A), GPS_numSat, GPS_coord[0], GPS_coord[1], failsafePhase (flags), rxSignalReceived (flags), rxFlightChannelsValid (flags)";

#[derive(Debug, Clone, Copy, Default)]
pub struct BlackboxCsvWriter;

impl FamilyWriter for BlackboxCsvWriter {
    fn format_id(&self) -> &'static str {
        "blackbox_decoded_csv"
    }
    fn extension(&self) -> &'static str {
        "csv"
    }
    fn source_role(&self) -> SourceRole {
        SourceRole::Fc
    }
    fn file_label(&self) -> &'static str {
        "blackbox"
    }
    fn render(&self, timeline: &Timeline, asset_id: &str) -> Result<Vec<u8>, WriteError> {
        let events = fc_events_for(
            timeline,
            asset_id,
            FcLogFormat::BlackboxCsv,
            "blackbox_decoded_csv",
        )?;
        let rows: Vec<&crate::Event> = events
            .into_iter()
            .filter(|e| matches!(e.kind, EventKind::BlackboxRow { .. }))
            .collect();
        if rows.is_empty() {
            return Err(WriteError::NoEvents {
                asset_id: asset_id.to_string(),
                source: SourceRole::Fc,
            });
        }
        let with_rssi = matches!(rows[0].kind, EventKind::BlackboxRow { rssi: Some(_), .. });
        let mut s = String::new();
        s.push_str(if with_rssi {
            HEADER_WITH_RSSI
        } else {
            HEADER_NO_RSSI
        });
        s.push('\n');
        for e in rows {
            if e.boot_epoch != 0 {
                break; // 再起動後は新 header の別 log（本 writer は初回 boot だけ）。
            }
            let EventKind::BlackboxRow {
                loop_iteration,
                rc_command,
                vbat_v,
                amperage_a,
                rssi,
                gps_num_sat,
                gps_deg,
                failsafe_phase,
                rx_signal_received,
                rx_flight_channels_valid,
            } = &e.kind
            else {
                continue;
            };
            let (la, lo) = gps_deg.unwrap_or((0.0, 0.0));
            s.push_str(&format!(
                "{loop_iteration}, {}, {}, {}, {}, {}, {vbat_v:.2}, {amperage_a:.2}, ",
                e.t_boot_us, rc_command[0], rc_command[1], rc_command[2], rc_command[3]
            ));
            if with_rssi {
                s.push_str(&format!("{}, ", rssi.unwrap_or(0)));
            }
            s.push_str(&format!(
                "{gps_num_sat}, {la:.7}, {lo:.7}, {failsafe_phase}, {}, {}\n",
                u8::from(*rx_signal_received),
                u8::from(*rx_flight_channels_valid)
            ));
        }
        Ok(s.into_bytes())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_scenario::{FailureKind, Family, LinkProfile, generate, pre_demo_default};

    #[test]
    fn blackbox_csv_writer_renders_decoded_shape() {
        let tl = generate(&pre_demo_default(1));
        let bytes = BlackboxCsvWriter.render(&tl, "fpv-01").expect("P-03");
        assert!(String::from_utf8_lossy(&bytes).starts_with("loopIteration, time"));
    }

    #[test]
    fn rc_loss_rows_show_bf_timeouts_and_fiber_profile_drops_rssi_column() {
        let mut s = pre_demo_default(1);
        s.injections.clear();
        s.inject(FailureKind::RcLinkLoss, 70_000, 20_000, Family::Fpv);
        let tl = generate(&s);
        let csv =
            String::from_utf8(BlackboxCsvWriter.render(&tl, "fpv-01").expect("csv")).expect("utf8");
        let lines: Vec<&str> = csv.lines().collect();
        assert_eq!(lines[0], HEADER_WITH_RSSI);
        let row = |t_ms: usize| {
            lines[1 + t_ms / 100]
                .split(", ")
                .map(str::to_string)
                .collect::<Vec<_>>()
        };
        let r702 = row(70_200);
        assert_eq!(r702[12], "1", "failsafePhase");
        assert_eq!(r702[13], "0", "rxSignalReceived");
        assert_eq!(row(70_300)[8], "0", "rssi forced 0 at +250 ms");
        assert_eq!(row(71_500)[12], "2", "stage2 at +1.5 s");
        s.assets[2].link_profile = LinkProfile::Fiber;
        let tl = generate(&s);
        let csv =
            String::from_utf8(BlackboxCsvWriter.render(&tl, "fpv-01").expect("csv")).expect("utf8");
        assert!(csv.starts_with(HEADER_NO_RSSI));
    }
}
