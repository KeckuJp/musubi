#![allow(clippy::doc_markdown, clippy::expect_used, clippy::unwrap_used)]

use musubi_reference_pipeline::export::absences_csv;
use musubi_reference_pipeline::{
    InputFile, Knowledge, ObservationWindow, PipelineOut, SourceRun, analyze,
};
use musubi_reference_readers::profile::parse_profile;
use musubi_reference_types::{Family, FamilyProfile, SignatureCatalog, SourceRole};

const BATTERY_CASE: &str =
    include_str!("fixtures/unknown-adapter--ardupilot-battery--profile.toml");

const ASK_WINDOWED: &str = "\n[[expectations]]\nid = \"battery_1hz\"\nchannel = \"onboard\"\ncadence_ms = 1000\ngrace_k = 2\nrequired_fields = [\"battery_voltage_v\"]\nwindow_rule = \"declared_wall_window\"\n";
const ASK_PLAIN: &str = "\n[[expectations]]\nid = \"battery_1hz\"\nchannel = \"onboard\"\ncadence_ms = 1000\ngrace_k = 2\nrequired_fields = [\"battery_voltage_v\"]\n";

const CSV: &[u8] =
    b"TimeUS,battery_voltage_v\n0,24.5\n1000000,24.4\n2000000,24.3\n3000000,24.2\n30000000,23.0\n";

fn profile(ask: &str, basis: &str) -> FamilyProfile {
    let src = BATTERY_CASE.replace(
        "default_clock_basis = \"boot_relative\"",
        &format!("default_clock_basis = \"{basis}\""),
    );
    parse_profile(&format!("{src}{ask}"), "public").expect("adopted case profile plus one ask")
}

fn run(p: FamilyProfile, t0_ms: i64, end_ms: i64) -> Result<PipelineOut, String> {
    let knowledge = Knowledge {
        profiles: vec![p],
        catalog: SignatureCatalog {
            version: "test".into(),
            signatures: vec![],
        },
    };
    analyze(
        vec![InputFile {
            asset_id: "bat-01".into(),
            family: Family::FixedWing,
            source: SourceRole::RecordedExport,
            format_id: "telemetry_csv_us".into(),
            file_name: "battery.csv".into(),
            bytes: CSV.to_vec(),
        }],
        &knowledge,
        ObservationWindow::Explicit { t0_ms, end_ms },
    )
}

fn only_source(out: &PipelineOut) -> &SourceRun {
    &out.assets[0].sources[0]
}

#[test]
fn a_record_with_an_unknown_clock_is_refused_rather_than_answered_emptily() {
    let refused = run(profile(ASK_WINDOWED, "unknown"), 1_000, 4_000)
        .expect_err("an unanswerable question must not look like a clean result");
    assert!(refused.contains("clock basis is unknown"), "{refused}");
    assert!(
        refused.contains("does not give an unknown clock a duration"),
        "{refused}"
    );
    assert!(
        refused.contains("battery.csv"),
        "names the record: {refused}"
    );

    let ok = run(profile(ASK_PLAIN, "unknown"), 1_000, 4_000).expect("legacy path unchanged");
    assert_eq!(only_source(&ok).obs.len(), 5);
}

#[test]
fn the_declared_window_reaches_the_common_output_without_being_widened() {
    let windowed = run(profile(ASK_WINDOWED, "boot_relative"), 0, 4_000).expect("analyses");
    let src = only_source(&windowed);
    assert_eq!(
        src.obs.len(),
        5,
        "out-of-window rows are retained, not deleted"
    );

    assert!(
        src.negatives.is_empty(),
        "unexpected absences: {:?}",
        src.negatives
            .iter()
            .map(|n| (n.absent_since_ms, n.mark.reason_code.clone()))
            .collect::<Vec<_>>()
    );

    let plain = run(profile(ASK_PLAIN, "boot_relative"), 0, 4_000).expect("analyses");
    let widened = only_source(&plain);
    assert_eq!(
        widened.negatives.len(),
        1,
        "the legacy derivation still reaches past the declared window"
    );
    assert_eq!(widened.negatives[0].absent_since_ms, 3_000);

    let csv = absences_csv(&windowed);
    assert!(csv.starts_with("family,asset_id,source_role,profile_id"));
    assert_eq!(
        csv.lines().count(),
        1,
        "header only: nothing missing in the window"
    );
}

#[test]
fn a_gap_inside_the_declared_window_is_reported_with_in_window_evidence_only() {
    let out = run(profile(ASK_WINDOWED, "boot_relative"), 0, 20_000).expect("analyses");
    let src = only_source(&out);
    assert_eq!(src.negatives.len(), 1);
    let neg = &src.negatives[0];
    assert_eq!(neg.absent_since_ms, 3_000, "the last in-window row");
    assert_eq!(neg.expected_by_ms, 5_000);
    assert!(neg.last_good.is_some(), "evidence from an in-window row");
    assert!(
        neg.mark
            .reason_code
            .ends_with("|gap-content:no-record-in-gap"),
        "{}",
        neg.mark.reason_code
    );
    assert_eq!(
        src.gap_bounds[0].end,
        musubi_reference_readers::absence::GapEnd::OpenEnd
    );
    assert_eq!(src.gap_bounds[0].next_good, None);
    assert!(src.obs.iter().any(|o| o.t_ms == 30_000));

    let csv = absences_csv(&out);
    assert_eq!(csv.lines().count(), 2);
    assert!(csv.contains("gap-content:no-record-in-gap"), "{csv}");
}

#[test]
fn a_wall_window_is_mapped_onto_a_boot_axis_rather_than_used_as_raw_record_times() {
    const OFFSET_CSV: &[u8] = b"TimeUS,battery_voltage_v\n100000000,24.5\n101000000,24.4\n102000000,24.3\n103000000,24.2\n130000000,23.0\n";
    const T0_MS: i64 = 1_788_166_800_000;

    let knowledge = Knowledge {
        profiles: vec![profile(ASK_WINDOWED, "boot_relative")],
        catalog: SignatureCatalog {
            version: "test".into(),
            signatures: vec![],
        },
    };
    let out = analyze(
        vec![InputFile {
            asset_id: "bat-01".into(),
            family: Family::FixedWing,
            source: SourceRole::RecordedExport,
            format_id: "telemetry_csv_us".into(),
            file_name: "offset-battery.csv".into(),
            bytes: OFFSET_CSV.to_vec(),
        }],
        &knowledge,
        ObservationWindow::Explicit {
            t0_ms: T0_MS,
            end_ms: T0_MS + 4_000,
        },
    )
    .expect("analyses");

    let src = only_source(&out);
    assert!(
        src.negatives.is_empty(),
        "the wall window was not mapped onto the record's axis: {:?}",
        src.negatives
            .iter()
            .map(|n| (n.absent_since_ms, n.mark.reason_code.clone()))
            .collect::<Vec<_>>()
    );
    assert_eq!(
        src.obs.len(),
        5,
        "the 130 s row is retained, just not consulted"
    );
    assert!(
        src.alignment.offset.is_some(),
        "the mapping needs an offset, and its absence is a refusal, not a guess"
    );
}
