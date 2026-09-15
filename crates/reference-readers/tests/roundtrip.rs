use musubi_reference_readers::FamilyProfile;
use musubi_reference_readers::absence::detect_absences;
use musubi_reference_readers::profile::load_profiles;
use musubi_reference_readers::time_align::{estimate_alignment, promote};
use musubi_reference_readers::{ChannelId, ClockBasis, FieldValue, reader_for};
use musubi_reference_scenario::{FailureKind, Family, FcLogFormat, generate, pre_demo_default};
use musubi_reference_writers::FamilyWriter;
use musubi_reference_writers::bin::ArduPilotBinWriter;
use musubi_reference_writers::blackbox::BlackboxCsvWriter;
use musubi_reference_writers::edgetx::EdgeTxCsvWriter;
use musubi_reference_writers::tlog::TlogWriter;
use musubi_reference_writers::ulog::Px4UlgWriter;
use musubi_reference_writers::video::VideoPresenceWriter;

fn public_profiles() -> Vec<FamilyProfile> {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    load_profiles(&root.join("profiles/public"), None).expect("public profiles load")
}

fn profile(id: &str) -> FamilyProfile {
    public_profiles()
        .into_iter()
        .find(|p| p.profile_id == id)
        .unwrap_or_else(|| panic!("profile {id} exists in profiles/public"))
}

#[test]
fn tlog_round_trip_detects_heartbeat_absence_in_injected_window() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.inject(FailureKind::TelemetryLinkLoss, 60_000, 30_000, Family::Ugv);
    let tl = generate(&s);
    let bytes = TlogWriter.render(&tl, "ugv-01").expect("tlog rendered");

    let p = profile("ardupilot_rover_tlog");
    let reader = reader_for(&p.format).expect("tlog reader registered");
    let obs = reader.read(&p, &bytes).expect("tlog parses");
    assert!(!obs.is_empty());
    assert!(
        obs.iter()
            .all(|o| o.clock_basis == ClockBasis::HostReceived)
    );
    let t0_ms = tl.t0_unix_us / 1000;
    let end_ms = t0_ms + s.duration_ms as i64;
    let neg = detect_absences(&p, "ugv-01", &obs, t0_ms, end_ms);

    let hb: Vec<_> = neg
        .iter()
        .filter(|n| n.subject.channel == ChannelId::Heartbeat)
        .collect();
    assert_eq!(hb.len(), 1, "exactly one heartbeat gap: {neg:?}");
    let n = hb[0];
    assert_eq!(n.absent_since_ms, t0_ms + 59_000);
    assert_eq!(n.expected_by_ms, t0_ms + 62_000);
    assert!(n.last_good.is_some());
    assert_eq!(n.clock_basis, ClockBasis::HostReceived);
    assert_eq!(
        n.mark.reason_code,
        "absence-expected-cadence|clock-basis:host_received"
    );
    assert!(n.expectation_id.starts_with("heartbeat_1hz@"));
    assert!(
        neg.iter()
            .any(|n| n.subject.channel == ChannelId::LinkStats)
    );
    assert!(neg.iter().any(|n| n.subject.channel == ChannelId::GpsEkf));
    assert!(neg.iter().any(|n| n.subject.channel == ChannelId::Rc));
    let al = estimate_alignment(&obs, p.default_clock_basis);
    assert_eq!(al.basis, ClockBasis::HostReceived);
    let off = al.offset.expect("vehicle offset from SYSTEM_TIME");
    assert!(
        (off.offset_us - (tl.t0_unix_us - 37_000_000)).abs() < 10_000,
        "{off:?}"
    );
}

#[test]
fn edgetx_round_trip_detects_link_stats_absence_from_zero_rows() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.inject(FailureKind::RcLinkLoss, 70_000, 20_000, Family::Fpv);
    let tl = generate(&s);
    let bytes = EdgeTxCsvWriter.render(&tl, "fpv-01").expect("csv rendered");

    let p = profile("edgetx_handset_csv");
    let reader = reader_for(&p.format).expect("edgetx reader registered");
    let obs = reader.read(&p, &bytes).expect("csv parses");
    assert_eq!(obs.len(), 181);
    let stale = obs.iter().filter(|o| o.stale).count();
    assert_eq!(stale, 20, "20 zero rows with GPS blank");
    let t0_ms = tl.t0_unix_us / 1000;
    let neg = detect_absences(&p, "fpv-01", &obs, t0_ms, t0_ms + s.duration_ms as i64);
    assert_eq!(neg.len(), 1, "{neg:?}");
    assert_eq!(neg[0].subject.channel, ChannelId::LinkStats);
    assert_eq!(neg[0].absent_since_ms, t0_ms + 69_000);
    assert_eq!(neg[0].expected_by_ms, t0_ms + 72_000);
}

#[test]
fn shipped_edgetx_profile_reads_rtc_less_tmr10ms_writer_output() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.assets
        .iter_mut()
        .find(|asset| asset.asset_id == "fpv-01")
        .expect("fpv asset")
        .handset_tmr10ms = true;
    let tl = generate(&s);
    let bytes = EdgeTxCsvWriter.render(&tl, "fpv-01").expect("csv rendered");

    let p = profile("edgetx_handset_csv");
    assert_eq!(p.fields.time, "Date,Time|Time|tmr10ms");
    let obs = reader_for(&p.format)
        .expect("edgetx reader registered")
        .read(&p, &bytes)
        .expect("public profile reads RTC-less writer output");

    assert_eq!(obs.len(), 181);
    assert_eq!(obs[0].t_ms, 30_000);
    assert!(
        obs.iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelative && o.wall_ms.is_none())
    );
}

#[test]
fn edgetx_frozen_variant_is_caught_by_frozen_ms_rule() {
    use musubi_reference_scenario::{HandsetLossMode, Injection};
    let mut s = pre_demo_default(11);
    s.injections.clear();
    let mut i = Injection::new(FailureKind::RcLinkLoss, 70_000, 60_000, Family::Fpv);
    i.params.handset_loss_mode = HandsetLossMode::Frozen;
    s.inject_with(i);
    let tl = generate(&s);
    let bytes = EdgeTxCsvWriter.render(&tl, "fpv-01").expect("csv rendered");
    let p = profile("edgetx_handset_csv");
    let obs = reader_for(&p.format)
        .expect("reader")
        .read(&p, &bytes)
        .expect("parses");
    assert_eq!(obs.iter().filter(|o| o.stale).count(), 0);
    let t0_ms = tl.t0_unix_us / 1000;
    let neg = detect_absences(&p, "fpv-01", &obs, t0_ms, t0_ms + s.duration_ms as i64);
    assert_eq!(neg.len(), 1, "{neg:?}");
    assert!(
        (t0_ms + 85_000..=t0_ms + 90_000).contains(&neg[0].absent_since_ms),
        "{neg:?}"
    );
}

#[test]
fn no_injection_yields_no_absence() {
    let mut s = pre_demo_default(5);
    s.injections.clear();
    let tl = generate(&s);
    let p = profile("ardupilot_plane_tlog");
    let bytes = TlogWriter.render(&tl, "plane-01").expect("tlog");
    let obs = reader_for(&p.format)
        .expect("reader")
        .read(&p, &bytes)
        .expect("parses");
    let t0_ms = tl.t0_unix_us / 1000;
    let neg = detect_absences(&p, "plane-01", &obs, t0_ms, t0_ms + s.duration_ms as i64);
    assert!(neg.is_empty(), "{neg:?}");
}

#[test]
fn all_public_profiles_parse_and_every_format_has_a_reader() {
    let ps = public_profiles();
    assert_eq!(
        ps.len(),
        8,
        "{:?}",
        ps.iter().map(|p| p.profile_id.clone()).collect::<Vec<_>>()
    );
    for p in &ps {
        assert_eq!(p.origin, "public");
        assert!(
            reader_for(&p.format).is_some(),
            "{}: no reader for {}",
            p.profile_id,
            p.format
        );
        assert!(
            !p.expectations.is_empty(),
            "{}: profile needs at least one expectation",
            p.profile_id
        );
    }
}

#[test]
fn tlog_field_values_survive_writer_reader_round_trip() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    let tl = generate(&s);
    let bytes = TlogWriter.render(&tl, "ugv-01").expect("tlog");
    let p = profile("ardupilot_rover_tlog");
    let obs = reader_for(&p.format)
        .expect("reader")
        .read(&p, &bytes)
        .expect("parses");
    let t0_ms = tl.t0_unix_us / 1000;
    let at = |t_ms: i64, key: &str| -> i64 {
        obs.iter()
            .filter(|o| o.t_ms == t0_ms + t_ms)
            .find_map(|o| {
                o.fields
                    .iter()
                    .find(|(k, _)| k == key)
                    .map(|(_, v)| v.clone())
            })
            .and_then(|v| match v {
                FieldValue::I64(i) => Some(i),
                _ => None,
            })
            .unwrap_or_else(|| panic!("{key} at +{t_ms} ms"))
    };
    assert_eq!(at(0, "SYS_STATUS.voltage_battery"), 12_400);
    assert_eq!(at(0, "SYS_STATUS.battery_remaining"), 95);
    assert_eq!(at(10_000, "SYS_STATUS.battery_remaining"), 94);
    assert_eq!(at(0, "GPS_RAW_INT.fix_type"), 3);
    assert!((14..=16).contains(&at(0, "GPS_RAW_INT.satellites_visible")));
    assert!((90..110).contains(&at(0, "GPS_RAW_INT.eph")));
    assert_eq!(at(0, "GPS_RAW_INT.time_usec"), 37_000_740);
    assert_eq!(at(0, "GLOBAL_POSITION_INT.time_boot_ms"), 37_000);
    assert_eq!(at(0, "SYSTEM_TIME.time_unix_usec"), tl.t0_unix_us);
    assert_eq!(at(0, "GLOBAL_POSITION_INT.lat"), 356_762_000);
    assert_eq!(at(0, "HEARTBEAT.type"), 10, "MAV_TYPE_GROUND_ROVER for ugv");
    assert_eq!(at(0, "HEARTBEAT.custom_mode"), 10);
    assert!((180..=189).contains(&at(0, "RADIO_STATUS.rssi")));
    assert_eq!(
        at(0, "RADIO_STATUS.remrssi"),
        at(0, "RADIO_STATUS.rssi") - 5
    );
    assert_eq!(at(0, "RADIO_STATUS.rxerrors"), 0);
    assert_eq!(at(0, "RC_CHANNELS.chancount"), 4);
}

#[test]
fn ardupilot_bin_round_trip_reads_fmt_driven_records_and_gps_anchors() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.inject(FailureKind::GnssDegradation, 120_000, 40_000, Family::Ugv);
    s.inject(FailureKind::RcLinkLoss, 60_000, 20_000, Family::Ugv);
    let tl = generate(&s);
    let bytes = ArduPilotBinWriter.render(&tl, "ugv-01").expect("bin");
    let p = profile("ardupilot_rover_bin");
    let obs = reader_for(&p.format)
        .expect("reader")
        .read(&p, &bytes)
        .expect("parses");
    assert!(obs.len() > 2_000);
    let al = estimate_alignment(&obs, p.default_clock_basis);
    assert_eq!(al.basis, ClockBasis::BootRelativeOffsetEstimated, "{al:?}");
    let off = al.offset.expect("offset");
    assert!(
        (off.offset_us - (tl.t0_unix_us - 37_000_740)).abs() < 5_000,
        "{off:?}"
    );
    let obs = promote(&obs, &al);
    assert!(
        obs.iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelativeOffsetEstimated)
    );
    let wall0 = obs
        .iter()
        .find(|o| o.t_boot_us == Some(37_000_740))
        .and_then(|o| o.wall_ms)
        .expect("wall");
    assert!(
        (wall0 - tl.t0_unix_us / 1000).abs() <= off.bound_us / 1000 + 1,
        "wall0 off by {} ms (bound {} us)",
        wall0 - tl.t0_unix_us / 1000,
        off.bound_us
    );
    let has = |k: &str, pred: &dyn Fn(&FieldValue) -> bool| {
        obs.iter()
            .any(|o| o.fields.iter().any(|(n, v)| n == k && pred(v)))
    };
    assert!(has("MODE.Rsn", &|v| *v == FieldValue::I64(25)));
    assert!(has("MSG.Message", &|v| *v
        == FieldValue::Text("Radio Failsafe".into())));
    assert!(!has("ERR.Subsys", &|v| *v == FieldValue::I64(5)));
    assert!(has("ERR.Subsys", &|v| *v == FieldValue::I64(11)));
    assert!(has("ERR.Subsys", &|v| *v == FieldValue::I64(17)));
    let first = obs.iter().map(|o| o.t_ms).min().unwrap();
    let last = obs.iter().map(|o| o.t_ms).max().unwrap();
    let neg = detect_absences(&p, "ugv-01", &obs, first, last);
    assert!(
        neg.iter().any(|n| n.subject.channel == ChannelId::Rc),
        "{neg:?}"
    );
    assert!(
        neg.iter().all(|n| n.subject.channel == ChannelId::Rc),
        "only rc: {neg:?}"
    );
}

#[test]
fn px4_ulg_round_trip_reads_topics_and_truncation_shows_as_tail_absence() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.inject(FailureKind::FcFailure, 90_000, 90_000, Family::FixedWing);
    let tl = generate(&s);
    let bytes = Px4UlgWriter.render(&tl, "plane-01").expect("ulg");
    let p = profile("px4_ulg");
    let obs = reader_for(&p.format)
        .expect("reader")
        .read(&p, &bytes)
        .expect("parses");
    assert!(
        obs.iter()
            .any(|o| o.fields.iter().any(|(k, _)| k == "sensor_gps.fix_type"))
    );
    assert!(
        obs.iter()
            .any(|o| o.fields.iter().any(|(k, _)| k == "input_rc.link_quality"))
    );
    let al = estimate_alignment(&obs, p.default_clock_basis);
    assert_eq!(al.basis, ClockBasis::GpsLocked, "{al:?}");
    let obs = promote(&obs, &al);
    let (first, _last) = (
        obs.iter().map(|o| o.t_ms).min().unwrap(),
        obs.iter().map(|o| o.t_ms).max().unwrap(),
    );
    let neg = detect_absences(&p, "plane-01", &obs, first, 123_000 + 180_000);
    assert!(
        neg.iter().any(|n| n.subject.channel == ChannelId::Onboard
            && n.absent_since_ms <= 213_000
            && n.absent_since_ms >= 212_000),
        "{neg:?}"
    );
}

#[test]
fn blackbox_and_video_round_trips_detect_rc_and_video_absence() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.inject(FailureKind::RcLinkLoss, 70_000, 20_000, Family::Fpv);
    s.inject(FailureKind::CameraStop, 120_000, 30_000, Family::Fpv);
    let tl = generate(&s);
    let bb = BlackboxCsvWriter.render(&tl, "fpv-01").expect("bb");
    let p = profile("betaflight_blackbox_csv");
    let obs = reader_for(&p.format)
        .expect("reader")
        .read(&p, &bb)
        .expect("parses");
    let first = obs.iter().map(|o| o.t_ms).min().unwrap();
    let last = obs.iter().map(|o| o.t_ms).max().unwrap();
    let neg = detect_absences(&p, "fpv-01", &obs, first, last);
    let rc: Vec<_> = neg
        .iter()
        .filter(|n| n.subject.channel == ChannelId::Rc)
        .collect();
    assert_eq!(rc.len(), 1, "{neg:?}");
    assert!(
        (8_000 + 70_000..=8_000 + 70_200).contains(&rc[0].absent_since_ms),
        "{rc:?}"
    );
    assert!(!neg.iter().any(|n| n.subject.channel == ChannelId::Onboard));
    let v = VideoPresenceWriter.render(&tl, "fpv-01").expect("video");
    let pv = profile("video_presence");
    let vobs = reader_for(&pv.format)
        .expect("reader")
        .read(&pv, &v)
        .expect("parses");
    let t0_ms = tl.t0_unix_us / 1000;
    let vneg = detect_absences(&pv, "fpv-01", &vobs, t0_ms, t0_ms + s.duration_ms as i64);
    assert_eq!(vneg.len(), 1, "{vneg:?}");
    assert_eq!(vneg[0].absent_since_ms, t0_ms + 119_000);
    assert_eq!(vneg[0].clock_basis, ClockBasis::Unknown);
}

#[test]
fn plane_bin_variant_round_trips_with_plane_profile() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.assets[1].fc_log = FcLogFormat::ArduPilotBin;
    s.inject(FailureKind::RcLinkLoss, 60_000, 20_000, Family::FixedWing);
    let tl = generate(&s);
    let bytes = ArduPilotBinWriter.render(&tl, "plane-01").expect("bin");
    let p = profile("ardupilot_plane_bin");
    let obs = reader_for(&p.format)
        .expect("reader")
        .read(&p, &bytes)
        .expect("parses");
    let has = |k: &str, v: FieldValue| {
        obs.iter()
            .any(|o| o.fields.iter().any(|(n, x)| n == k && *x == v))
    };
    assert!(has("MODE.Rsn", FieldValue::I64(3)));
    assert!(has(
        "MSG.Message",
        FieldValue::Text("RC Short Failsafe On".into())
    ));
    assert!(has(
        "MSG.Message",
        FieldValue::Text("RC Long Failsafe On: switched to RTL".into())
    ));
    assert!(!has("ERR.Subsys", FieldValue::I64(5)));
}
