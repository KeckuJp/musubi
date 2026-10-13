use musubi_reference_pipeline::ObservationWindow;
use musubi_reference_pipeline::tlog_asset_batch::{
    Binding, RecordedBinding, analyze_declared, analyze_recorded,
};
use musubi_reference_readers::{
    FieldValue, field, profile::parse_profile, tlog_sender_batch::Limits,
};
use musubi_reference_types::{ClockBasis, FamilyProfile, SignatureCatalog};

fn profile(id: &str, sys: u8, comp: u8, host: bool) -> FamilyProfile {
    let clock = if host { "host_received" } else { "unknown" };
    let time = if host { "host_us" } else { "host_recorded_us" };
    parse_profile(
        &format!(
            r#"profile_id = "{id}"
version = "1"
family = "unknown"
source_role = "gcs"
format = "mavlink_tlog"
extensions = ["tlog"]
default_clock_basis = "{clock}"
[fields]
time = "{time}"
[selected_sender]
system_id = {sys}
component_id = {comp}
"#
        ),
        "public",
    )
    .unwrap()
}

fn frame(sys: u8, comp: u8, message: u8) -> Vec<u8> {
    let mut payload = vec![0; if message == 24 { 30 } else { 1 }];
    if message == 24 {
        payload[..8].copy_from_slice(&1000_u64.to_le_bytes());
        payload[8..12].copy_from_slice(&(i32::from(sys) * 1_000_000).to_le_bytes());
        payload[28] = 3;
    }
    let mut wire = vec![0xfe, payload.len() as u8, 7, sys, comp, message];
    wire.extend(payload);
    let extra = if message == 24 { 24 } else { 0 };
    let mut crc = 0xffff_u16;
    for byte in wire[1..].iter().chain(std::iter::once(&extra)) {
        let mut t = byte ^ (crc & 0xff) as u8;
        t ^= t << 4;
        crc = (crc >> 8) ^ (u16::from(t) << 8) ^ (u16::from(t) << 3) ^ (u16::from(t) >> 4);
    }
    wire.extend(crc.to_le_bytes());
    let mut result = 1_700_000_000_100_000_u64.to_be_bytes().to_vec();
    result.extend(wire);
    result
}

fn catalog() -> SignatureCatalog {
    SignatureCatalog {
        version: "authored".into(),
        signatures: vec![],
    }
}

#[test]
fn recorded_batch_retains_unknown_clock_all_records_and_separate_assets() {
    let bindings = [
        RecordedBinding {
            asset_id: "a".into(),
            profile: profile("a", 42, 3, false),
        },
        RecordedBinding {
            asset_id: "b".into(),
            profile: profile("b", 7, 190, false),
        },
    ];
    let bytes = [
        frame(42, 3, 24),
        frame(7, 190, 24),
        frame(9, 9, 24),
        frame(42, 3, 3),
    ]
    .concat();
    let output = analyze_recorded(
        &bindings,
        &bytes,
        &catalog(),
        1_700_000_000_000,
        1_700_000_001_000,
        Limits::default(),
    )
    .unwrap();
    assert_eq!(
        (
            output.reported.total_records,
            output.reported.selected_records,
            output.reported.unselected_records
        ),
        (4, 3, 1)
    );
    assert_eq!(output.analyses.len(), 2);
    for (i, analysis) in output.analyses.iter().enumerate() {
        assert_eq!(analysis.assets.len(), 1);
        assert_eq!(analysis.assets[0].asset_id, bindings[i].asset_id);
        let observations = &analysis.assets[0].sources[0].obs;
        assert_eq!(observations.len(), if i == 0 { 2 } else { 1 });
        for observation in observations {
            assert_eq!(observation.clock_basis, ClockBasis::Unknown);
            assert_eq!(observation.wall_ms, None);
            assert_eq!(
                field(observation, "MAVLINK.system_id"),
                Some(&FieldValue::I64(if i == 0 { 42 } else { 7 }))
            );
        }
    }
    assert!(
        field(
            output.analyses[0].assets[0].sources[0].obs.last().unwrap(),
            "MAVLINK.raw_frame_hex"
        )
        .is_some()
    );
    let mut duplicate = bindings.clone();
    duplicate[1].asset_id = "a".into();
    assert!(analyze_recorded(&duplicate, &bytes, &catalog(), 0, 1, Limits::default()).is_err());
    assert!(analyze_recorded(&bindings, &bytes, &catalog(), 2, 1, Limits::default()).is_err());
    let tight = Limits {
        aggregate_read_bytes: bytes.len() * 2,
        ..Limits::default()
    };
    assert!(analyze_recorded(&bindings, &bytes, &catalog(), 0, 1, tight).is_err());
}

#[test]
fn receipt_clock_requires_explicit_basis_and_absent_sender_refuses_whole_batch() {
    let bindings = [
        Binding {
            asset_id: "a".into(),
            profile: profile("a", 42, 3, true),
            host_received_basis: "authored receipt timestamps".into(),
        },
        Binding {
            asset_id: "b".into(),
            profile: profile("b", 7, 190, true),
            host_received_basis: "authored receipt timestamps".into(),
        },
    ];
    let bytes = [frame(42, 3, 24), frame(7, 190, 24)].concat();
    let window = ObservationWindow::Explicit {
        t0_ms: 1_700_000_000_000,
        end_ms: 1_700_000_001_000,
    };
    let output =
        analyze_declared(&bindings, &bytes, &catalog(), window, Limits::default()).unwrap();
    assert!(
        output
            .analyses
            .iter()
            .all(|a| a.assets[0].sources[0].obs[0].clock_basis == ClockBasis::HostReceived)
    );
    assert!(
        output
            .reported
            .senders
            .iter()
            .all(|s| s.observations[0].clock_basis == ClockBasis::Unknown)
    );
    let mut missing = bindings.clone();
    missing[0].host_received_basis.clear();
    assert!(analyze_declared(&missing, &bytes, &catalog(), window, Limits::default()).is_err());
    assert!(
        analyze_declared(
            &bindings,
            &frame(42, 3, 24),
            &catalog(),
            window,
            Limits::default()
        )
        .is_err()
    );
}
