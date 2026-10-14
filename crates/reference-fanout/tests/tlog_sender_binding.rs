//! Which reported sender a recorded TLOG is read as, through the **existing** route:
//! reader to `analyze` to `build_steps` to the sealed `PlatformState`.
//!
//! One saved file can carry several reported MAVLink senders. The reader has always kept each
//! row's `MAVLINK.system_id`/`component_id`, but the pipeline groups by the caller-declared
//! `InputFile.asset_id`, so without a selection two independent senders land in one `AssetRun`
//! and the fan-out seals one position per asset from whichever of them reported last. This test
//! pins both halves: the merge that happens by default, and the separation a declared selection
//! gives.
//!
//! Authored bytes only - no device, broker or live link, and no authenticated identity: the
//! selected tuple is a reported header value and `asset_id` is the operator's own label.
use musubi_reference_fanout::build_steps;
use musubi_reference_pipeline::{InputFile, Knowledge, ObservationWindow, PipelineOut, analyze};
use musubi_reference_readers::FieldValue;
use musubi_reference_readers::profile::parse_profile;
use musubi_reference_types::{Family, FamilyProfile, SignatureCatalog, SourceRole};

const BASE_PROFILE: &str = r#"
profile_id = "tlog_all_senders_v1"
version = "1"
family = "ugv"
source_role = "gcs"
format = "mavlink_tlog"
extensions = ["tlog"]
default_clock_basis = "host_received"
[fields]
time = "host_us"
"#;

/// MAVLink CRC-16/MCRF4XX over the frame after the magic, then the message CRC extra.
/// Authored here so this test adds no dependency; the reader validates it, so a wrong
/// implementation would fail the read rather than pass silently.
fn seal(frame: &mut Vec<u8>, crc_extra: u8) {
    let mut crc = 0xFFFF_u16;
    for &b in frame[1..].iter().chain(std::iter::once(&crc_extra)) {
        let mut tmp = b ^ (crc & 0xFF) as u8;
        tmp ^= tmp << 4;
        crc = (crc >> 8) ^ (u16::from(tmp) << 8) ^ (u16::from(tmp) << 3) ^ (u16::from(tmp) >> 4);
    }
    frame.extend_from_slice(&crc.to_le_bytes());
}

/// GPS_RAW_INT (msgid 24, CRC extra 24) payload carrying the reported degE7 coordinates.
fn gps_payload(lat_e7: i32, lon_e7: i32) -> Vec<u8> {
    let mut p = vec![0u8; 30];
    p[..8].copy_from_slice(&1_500_000_u64.to_le_bytes());
    p[8..12].copy_from_slice(&lat_e7.to_le_bytes());
    p[12..16].copy_from_slice(&lon_e7.to_le_bytes());
    p[20..22].copy_from_slice(&120_u16.to_le_bytes());
    p[24..26].copy_from_slice(&1234_u16.to_le_bytes());
    p[28] = 3;
    p[29] = 11;
    p
}

/// One TLOG record: 8-byte big-endian host microseconds, then this sender's MAVLink1 frame.
/// Both senders deliberately reuse packet sequence 7, so only the reported tuple separates them.
fn record(host_us: u64, sysid: u8, compid: u8, lat_e7: i32, lon_e7: i32) -> Vec<u8> {
    let payload = gps_payload(lat_e7, lon_e7);
    let mut out = host_us.to_be_bytes().to_vec();
    let mut frame = vec![0xFE, payload.len() as u8, 7, sysid, compid, 24];
    frame.extend_from_slice(&payload);
    seal(&mut frame, 24);
    out.extend_from_slice(&frame);
    out
}

/// One saved file: sender A first, sender B last in host time, then A again.
fn mixed_recording() -> Vec<u8> {
    [
        record(1_700_000_000_100_000, 42, 3, 356_812_345, 1_396_500_000),
        record(1_700_000_000_200_000, 7, 190, -338_675_000, 1_512_090_000),
        record(1_700_000_000_300_000, 42, 3, 356_812_400, 1_396_500_100),
    ]
    .concat()
}

fn selecting(id: &str, system_id: u8, component_id: u8) -> FamilyProfile {
    let toml = format!(
        "{BASE_PROFILE}[selected_sender]\nsystem_id = {system_id}\ncomponent_id = {component_id}\n"
    );
    let mut profile = parse_profile(&toml, "public").expect("authored profile");
    profile.profile_id = id.into();
    profile
}

fn input(asset_id: &str) -> InputFile {
    InputFile {
        asset_id: asset_id.into(),
        family: Family::Ugv,
        source: SourceRole::Gcs,
        format_id: "mavlink_tlog".into(),
        file_name: "recorded.tlog".into(),
        bytes: mixed_recording(),
    }
}

fn analyzed(profiles: Vec<FamilyProfile>, files: Vec<InputFile>) -> Result<PipelineOut, String> {
    analyze(
        files,
        &Knowledge {
            profiles,
            catalog: SignatureCatalog {
                version: "test".into(),
                signatures: vec![],
            },
        },
        ObservationWindow::Derived,
    )
}

fn senders(out: &PipelineOut) -> Vec<(i64, i64)> {
    out.assets[0].sources[0]
        .obs
        .iter()
        .map(|o| {
            let pick = |name: &str| match o.fields.iter().find(|(n, _)| n == name) {
                Some((_, FieldValue::I64(v))) => *v,
                other => panic!("missing reported sender id: {other:?}"),
            };
            (pick("MAVLINK.system_id"), pick("MAVLINK.component_id"))
        })
        .collect()
}

/// Sealed (asset, latitude) pairs the existing fan-out derives.
fn sealed(out: &PipelineOut) -> Vec<(String, f64)> {
    build_steps(out)
        .iter()
        .filter_map(|s| s.position().map(|p| (s.asset_id.clone(), p.lat_deg)))
        .collect()
}

fn batch_binding(
    asset: &str,
    sys: u8,
    comp: u8,
) -> musubi_reference_pipeline::tlog_asset_batch::Binding {
    let profile = selecting(asset, sys, comp);
    musubi_reference_pipeline::tlog_asset_batch::Binding { asset_id: asset.into(), profile,
        host_received_basis:"Authored fixture explicitly models receipt timestamps, not a real bidirectional GCS log".into() }
}

#[test]
fn unknown_recorded_positions_preserve_assets_order_clock_and_nonposition_count() {
    use musubi_reference_pipeline::tlog_asset_batch::{RecordedBinding, analyze_recorded};
    use musubi_reference_readers::tlog_sender_batch::Limits;
    let catalog = SignatureCatalog {
        version: "test".into(),
        signatures: vec![],
    };
    let bindings: Vec<_> = [(42, 3), (7, 190)]
        .into_iter()
        .enumerate()
        .map(|(i, (sys, comp))| {
            let mut profile = selecting(&format!("recorded-{i}"), sys, comp);
            profile.fields.time = "host_recorded_us".into();
            profile.default_clock_basis = musubi_reference_types::ClockBasis::Unknown;
            RecordedBinding {
                asset_id: format!("recorded-{i}"),
                profile,
            }
        })
        .collect();
    for (lat_a, lat_b) in [(100_000_000, -200_000_000), (350_000_000, 400_000_000)] {
        let mut bytes = [
            record(1_700_000_000_100_000, 42, 3, lat_a, 300_000_000),
            record(1_700_000_000_100_000, 7, 190, lat_b, 400_000_000),
            record(1_700_000_000_200_000, 9, 9, 500_000_000, 600_000_000),
        ]
        .concat();
        let mut frame = vec![0xfe, 1, 8, 42, 3, 3, 0x11];
        seal(&mut frame, 0);
        bytes.extend(1_700_000_000_300_000_u64.to_be_bytes());
        bytes.extend(frame);
        let out = analyze_recorded(
            &bindings,
            &bytes,
            &catalog,
            1_700_000_000_000,
            1_700_000_001_000,
            Limits::default(),
        )
        .unwrap();
        assert_eq!(
            (
                out.reported.total_records,
                out.reported.selected_records,
                out.reported.unselected_records
            ),
            (4, 3, 1)
        );
        for (i, analysis) in out.analyses.iter().enumerate() {
            let positions = musubi_reference_fanout::recorded_positions(analysis);
            assert_eq!(positions.observations, if i == 0 { 2 } else { 1 });
            assert_eq!(positions.without_qualified_position, usize::from(i == 0));
            assert_eq!(positions.positions.len(), 1);
            let selected = &positions.positions[0];
            assert_eq!(selected.asset_id, bindings[i].asset_id);
            assert_eq!((selected.source_index, selected.observation_index), (0, 0));
            assert!(std::ptr::eq(
                selected.observation,
                &analysis.assets[0].sources[0].obs[0]
            ));
            assert_eq!(
                selected.observation.clock_basis,
                musubi_reference_types::ClockBasis::Unknown
            );
            assert_eq!(selected.observation.wall_ms, None);
            assert_eq!(selected.observation.t_ms, 1_700_000_000_100);
            assert_eq!(
                selected.position.lat_deg,
                f64::from(if i == 0 { lat_a } else { lat_b }) / 1e7
            );
            assert!(build_steps(analysis).is_empty());
        }
        let unknown = out.analyses[0].assets[0].sources[0].obs.last().unwrap();
        assert!(musubi_reference_readers::field(unknown, "MAVLINK.raw_frame_hex").is_some());
        assert!(analyze_recorded(&bindings, &bytes, &catalog, 2, 1, Limits::default()).is_err());
        let mut host = bindings.clone();
        host[0].profile.default_clock_basis = musubi_reference_types::ClockBasis::HostReceived;
        assert!(analyze_recorded(&host, &bytes, &catalog, 0, 1, Limits::default()).is_err());
    }
}

#[test]
fn declared_batch_reaches_separate_asset_analyses() {
    use musubi_reference_pipeline::tlog_asset_batch::analyze_declared;
    use musubi_reference_readers::tlog_sender_batch::Limits;
    let catalog = SignatureCatalog {
        version: "test".into(),
        signatures: vec![],
    };
    let bindings = [
        batch_binding("asset-a", 42, 3),
        batch_binding("asset-b", 7, 190),
    ];
    for (case, mut bytes) in [
        mixed_recording(),
        [
            record(1_700_000_001_000_000, 42, 3, 100_000_000, 200_000_000),
            record(1_700_000_001_100_000, 7, 190, -200_000_000, 300_000_000),
        ]
        .concat(),
    ]
    .into_iter()
    .enumerate()
    {
        let expected_total = if case == 0 { 4 } else { 3 };
        let mut unknown = 1_700_000_001_200_000_u64.to_be_bytes().to_vec();
        let mut frame = vec![0xfe, 1, 8, 42, 3, 3, 0x11];
        seal(&mut frame, 0);
        unknown.extend(frame);
        bytes.extend(unknown);
        let out = analyze_declared(
            &bindings,
            &bytes,
            &catalog,
            ObservationWindow::Explicit {
                t0_ms: 1_700_000_000_000,
                end_ms: 1_700_000_002_000,
            },
            Limits::default(),
        )
        .unwrap();
        assert_eq!(out.reported.total_records, expected_total);
        assert_eq!(out.reported.selected_records, out.reported.total_records);
        assert_eq!(out.reported.unselected_records, 0);
        assert_eq!(out.analyses.len(), 2);
        for (index, analysis) in out.analyses.iter().enumerate() {
            assert_eq!(analysis.assets.len(), 1);
            assert_eq!(analysis.assets[0].asset_id, bindings[index].asset_id);
            let expected = if index == 0 { (42, 3) } else { (7, 190) };
            assert!(senders(analysis).iter().all(|pair| *pair == expected));
            assert_eq!(
                analysis.assets[0].sources[0].obs.len(),
                out.reported.senders[index].observations.len()
            );
            assert!(
                analysis.assets[0].sources[0]
                    .obs
                    .iter()
                    .all(|row| row.clock_basis == musubi_reference_types::ClockBasis::HostReceived)
            );
            assert!(
                out.reported.senders[index]
                    .observations
                    .iter()
                    .all(|row| row.clock_basis == musubi_reference_types::ClockBasis::Unknown)
            );
            assert_eq!(
                out.bindings[index].host_received_basis,
                bindings[index].host_received_basis
            );
            assert!(!sealed(analysis).is_empty());
            let expected_lat = if case == 0 {
                if index == 0 { 35.6812345 } else { -33.8675 }
            } else if index == 0 {
                10.
            } else {
                -20.
            };
            assert!(
                sealed(analysis)
                    .iter()
                    .any(|(asset, lat)| asset == &bindings[index].asset_id
                        && (lat - expected_lat).abs() < 1e-8)
            );
        }
        let unknown = out.analyses[0].assets[0].sources[0].obs.last().unwrap();
        assert_eq!(
            musubi_reference_readers::field(unknown, "MAVLINK.message_id"),
            Some(&FieldValue::I64(3))
        );
        assert!(musubi_reference_readers::field(unknown, "MAVLINK.raw_frame_hex").is_some());
    }
}

#[test]
fn declared_batch_refuses_ambiguous_labels_and_partial_success() {
    use musubi_reference_pipeline::tlog_asset_batch::analyze_declared;
    use musubi_reference_readers::tlog_sender_batch::Limits;
    let catalog = SignatureCatalog {
        version: "test".into(),
        signatures: vec![],
    };
    let bytes = mixed_recording();
    for bindings in [
        vec![batch_binding("same", 42, 3), batch_binding("same", 7, 190)],
        vec![batch_binding("a", 42, 3), batch_binding("b", 42, 3)],
        vec![batch_binding("a", 42, 3), batch_binding("b", 9, 9)],
        vec![batch_binding(" ", 42, 3)],
    ] {
        assert!(
            analyze_declared(
                &bindings,
                &bytes,
                &catalog,
                ObservationWindow::Derived,
                Limits::default()
            )
            .is_err()
        );
    }
    let bindings = [batch_binding("a", 42, 3), batch_binding("b", 7, 190)];
    let mut unknown = bindings.clone();
    unknown[0].profile.default_clock_basis = musubi_reference_types::ClockBasis::Unknown;
    assert!(
        analyze_declared(
            &unknown,
            &bytes,
            &catalog,
            ObservationWindow::Derived,
            Limits::default()
        )
        .is_err()
    );
    let mut missing_basis = bindings.clone();
    missing_basis[0].host_received_basis.clear();
    assert!(
        analyze_declared(
            &missing_basis,
            &bytes,
            &catalog,
            ObservationWindow::Derived,
            Limits::default()
        )
        .is_err()
    );
    let limited = Limits {
        aggregate_read_bytes: bytes.len() * 2,
        ..Limits::default()
    };
    assert!(
        analyze_declared(
            &bindings,
            &bytes,
            &catalog,
            ObservationWindow::Derived,
            limited
        )
        .is_err()
    );
    let mut corrupt = bytes;
    *corrupt.last_mut().unwrap() ^= 1;
    assert!(
        analyze_declared(
            &bindings,
            &corrupt,
            &catalog,
            ObservationWindow::Derived,
            Limits::default()
        )
        .is_err()
    );
}

/// Default behaviour, stated rather than assumed: one asset, three records from two senders,
/// and one sealed position that belongs to whichever sender reported last.
#[test]
fn without_a_selection_two_senders_share_one_asset_and_one_sealed_position() {
    let out = analyzed(
        vec![parse_profile(BASE_PROFILE, "public").expect("authored profile")],
        vec![input("asset-01")],
    )
    .expect("one profile binds");
    assert_eq!(out.assets.len(), 1);
    assert_eq!(senders(&out), [(42, 3), (7, 190), (42, 3)]);
    // No selection, so no selected-read accounting appears: the default output is unchanged.
    for o in &out.assets[0].sources[0].obs {
        for name in [
            "MAVLINK.declared_sender_selection",
            "MAVLINK.selected_sender_records",
            "MAVLINK.unselected_sender_records",
            "MAVLINK.selected_sender_record_scope",
        ] {
            assert!(!o.fields.iter().any(|(n, _)| n == name), "{name}");
        }
    }
    let sealed = sealed(&out);
    assert_eq!(sealed.len(), 1, "one asset seals one position: {sealed:?}");
    assert_eq!(sealed[0].0, "asset-01");
    // The rows stay distinct; the grouping does not. This is the residual being closed.
    assert!(
        (sealed[0].1 - 35.681_24).abs() < 0.001,
        "the last record of sender 42/3 wins the single position: {sealed:?}"
    );
}

/// With a declared selection each reported sender is read into its own asset and never mixes.
#[test]
fn a_declared_selection_keeps_each_reported_sender_in_its_own_asset() {
    let mut attributed = 0usize;
    for (asset, id, sysid, compid, expected, latitude) in [
        (
            "asset-sender-a",
            "tlog_sender_a_v1",
            42_u8,
            3_u8,
            vec![(42_i64, 3_i64), (42, 3)],
            35.681_24,
        ),
        (
            "asset-sender-b",
            "tlog_sender_b_v1",
            7,
            190,
            vec![(7, 190)],
            -33.867_5,
        ),
    ] {
        let out = analyzed(vec![selecting(id, sysid, compid)], vec![input(asset)])
            .expect("the selecting profile binds");
        assert_eq!(out.assets.len(), 1);
        assert_eq!(out.assets[0].asset_id, asset);
        assert_eq!(senders(&out), expected, "{asset}");
        // Every row says what this read is: a declared, reported subset - not a verified device.
        for o in &out.assets[0].sources[0].obs {
            let label = o
                .fields
                .iter()
                .find(|(n, _)| n == "MAVLINK.declared_sender_selection")
                .map(|(_, v)| v.clone());
            assert_eq!(
                label,
                Some(FieldValue::Text(format!(
                    "SELECTED_DECLARED_SENDER_{sysid}_{compid}_REPORTED_NOT_AUTHENTICATED_THIS_READ_IS_A_SUBSET_OF_THE_RECORDING"
                ))),
                "{asset}"
            );
        }
        // The whole-recording accounting travels with the read: the first row of this asset says
        // how many records this selection emitted and how many belong to other senders.
        let first = &out.assets[0].sources[0].obs[0];
        let count = |name: &str| match first.fields.iter().find(|(n, _)| n == name) {
            Some((_, FieldValue::I64(v))) => *v,
            other => panic!("missing selected-read count: {other:?}"),
        };
        assert_eq!(
            count("MAVLINK.selected_sender_records"),
            expected.len() as i64
        );
        assert_eq!(
            count("MAVLINK.unselected_sender_records"),
            3 - expected.len() as i64
        );
        let scope = first
            .fields
            .iter()
            .find(|(n, _)| n == "MAVLINK.selected_sender_record_scope")
            .map(|(_, v)| v.clone());
        assert_eq!(
            scope,
            Some(FieldValue::Text(
                "WHOLE_RECORDING_COUNTS_ON_THE_FIRST_SELECTED_ROW_EVERY_VALIDATED_RECORD_INCLUDING_UNKNOWN_MESSAGE_TYPES_COUNTED_ONCE_THIS_READ_IS_A_SUBSET_OF_THE_RECORDING"
                    .into()
            )),
            "{asset}"
        );
        // Later rows of the same read do not repeat the counts.
        for later in &out.assets[0].sources[0].obs[1..] {
            assert!(
                !later
                    .fields
                    .iter()
                    .any(|(n, _)| n == "MAVLINK.selected_sender_records"),
                "{asset}"
            );
        }
        // The sealed position of this asset is this sender's, never the other one's.
        let sealed = sealed(&out);
        assert_eq!(sealed.len(), 1, "{asset}: {sealed:?}");
        assert_eq!(sealed[0].0, asset);
        assert!(
            (sealed[0].1 - latitude).abs() < 0.001,
            "{asset}: {sealed:?}"
        );
        attributed += expected.len();
    }
    // No record is dropped and none is counted twice: the two declared reads partition the file.
    assert_eq!(attributed, 3);
}

/// Two selecting profiles cannot be bound to one file by first match: the existing ambiguity
/// refusal now covers a declared sender, so a selection is never silently applied to the wrong
/// recording. A selection naming an absent sender is refused rather than read as empty.
#[test]
fn a_selection_is_refused_rather_than_silently_first_matched_or_emptied() {
    let err = analyzed(
        vec![
            selecting("tlog_sender_a_v1", 42, 3),
            selecting("tlog_sender_b_v1", 7, 190),
        ],
        vec![input("asset-01")],
    )
    .expect_err("ambiguous binding");
    assert!(err.contains("ambiguous profile binding"), "{err}");
    assert!(err.contains("selected sender"), "{err}");
    let err = analyzed(
        vec![selecting("tlog_sender_x_v1", 9, 9)],
        vec![input("asset-01")],
    )
    .expect_err("absent sender");
    assert!(err.contains("reports no record in this recording"), "{err}");
}
