#![allow(clippy::doc_markdown, clippy::expect_used, clippy::unwrap_used)]

use musubi_reference_readers::absence::{detect_absences, detect_absences_with_bounds};
use musubi_reference_readers::profile::parse_profile;
use musubi_reference_readers::telemetry_csv::TelemetryCsvReader;
use musubi_reference_readers::{FieldValue, ProfileReader};

const BATTERY_CASE: &str =
    include_str!("fixtures/unknown-adapter--ardupilot-battery--profile.toml");
const ROVER_BIN: &str = include_str!("../../../profiles/public/ardupilot_rover_bin.toml");

const ASK: &str = "\n[[expectations]]\nid = \"battery_1hz\"\nchannel = \"onboard\"\ncadence_ms = 1000\ngrace_k = 2\nrequired_fields = [\"battery_voltage_v\", \"battery_temperature_k\"]\n";

fn reason(n: &musubi_reference_types::NegativeObservation) -> &str {
    &n.mark.reason_code
}

#[test]
fn a_required_field_that_is_blank_is_reported_missing_rather_than_read_as_zero() {
    let p = parse_profile(&format!("{BATTERY_CASE}{ASK}"), "public").expect("adopted case profile");
    let exp = &p.expectations[0];
    assert_eq!(exp.cadence_ms, 1_000);
    assert_eq!(exp.grace_k, 2);
    assert_eq!(
        exp.required_fields.as_deref(),
        Some(
            [
                "battery_voltage_v".to_string(),
                "battery_temperature_k".to_string()
            ]
            .as_slice()
        )
    );
    assert_eq!(exp.frozen_ms, None);

    let csv = "TimeUS,battery_voltage_v,battery_temperature_k\n\
               0,24.1,295.1\n\
               1000000,24.0,295.2\n\
               2000000,23.9,\n\
               3000000,23.8,\n\
               4000000,23.7,\n\
               5000000,23.6,295.3\n";
    let rows = TelemetryCsvReader.read(&p, csv.as_bytes()).expect("reads");
    assert_eq!(rows.len(), 6, "every row is read and kept");
    assert_eq!(
        rows[2]
            .fields
            .iter()
            .find(|(k, _)| k == "battery_temperature_k")
            .map(|(_, v)| v.clone()),
        Some(FieldValue::Blank)
    );

    let gaps = detect_absences_with_bounds(&p, "bat-01", &rows, 0, 5_000);
    assert_eq!(gaps.len(), 1, "one gap, from the last good row to the next");
    let (neg, bounds) = &gaps[0];
    assert_eq!(neg.absent_since_ms, 1_000, "the last row that carried both");
    assert_eq!(neg.expected_by_ms, 3_000, "cadence 1000 x grace 2");
    assert!(neg.last_good.is_some(), "evidence, not a bare assertion");
    assert_eq!(
        bounds.end,
        musubi_reference_readers::absence::GapEnd::NextObservation
    );
    assert!(bounds.next_good.is_some());
    assert_eq!(bounds.next_good_ms, Some(5_000));

    assert_eq!(
        reason(neg),
        "absence-expected-cadence|clock-basis:boot_relative|gap-content:required-field-absent"
    );
    assert_eq!(
        neg.mark.status,
        musubi_reference_types::MarkStatus::Degraded
    );
    assert_eq!(
        neg.expectation_id, "battery_1hz@1",
        "points back at the profile that asked"
    );
    assert_eq!(
        neg.mark.provenance,
        vec!["musubi-reference-readers/ardupilot_bat_finite_current_v1@1".to_string()]
    );
}

#[test]
fn an_absent_message_a_missing_field_a_stale_value_and_a_bad_file_stay_distinguishable() {
    let p = parse_profile(&format!("{BATTERY_CASE}{ASK}"), "public").expect("profile");
    let read = |csv: &str| TelemetryCsvReader.read(&p, csv.as_bytes());

    let absent_message =
        read("TimeUS,battery_voltage_v,battery_temperature_k\n0,24.1,295.1\n5000000,23.6,295.3\n")
            .expect("reads");
    let gaps = detect_absences(&p, "bat-01", &absent_message, 0, 5_000);
    assert_eq!(gaps.len(), 1);
    assert!(
        reason(&gaps[0]).ends_with("|gap-content:no-record-in-gap"),
        "{}",
        reason(&gaps[0])
    );

    let missing_field = read(
        "TimeUS,battery_voltage_v,battery_temperature_k\n0,24.1,295.1\n2000000,23.9,\n3000000,23.8,\n5000000,23.6,295.3\n",
    )
    .expect("reads");
    let gaps = detect_absences(&p, "bat-01", &missing_field, 0, 5_000);
    assert_eq!(gaps.len(), 1);
    assert!(
        reason(&gaps[0]).ends_with("|gap-content:required-field-absent"),
        "{}",
        reason(&gaps[0])
    );

    let mut stale_rows = read(
        "TimeUS,battery_voltage_v,battery_temperature_k\n0,24.1,295.1\n2000000,23.9,295.1\n3000000,23.8,295.1\n5000000,23.6,295.3\n",
    )
    .expect("reads");
    stale_rows[1].stale = true;
    stale_rows[2].stale = true;
    let gaps = detect_absences(&p, "bat-01", &stale_rows, 0, 5_000);
    assert_eq!(gaps.len(), 1);
    assert!(
        reason(&gaps[0]).ends_with("|gap-content:record-marked-stale"),
        "{}",
        reason(&gaps[0])
    );

    let mut mixed = read(
        "TimeUS,battery_voltage_v,battery_temperature_k\n0,24.1,295.1\n2000000,23.9,\n3000000,23.8,295.1\n5000000,23.6,295.3\n",
    )
    .expect("reads");
    mixed[1].stale = true; // this row is stale *and* missing the temperature
    mixed[2].stale = true;
    let gaps = detect_absences(&p, "bat-01", &mixed, 0, 5_000);
    assert_eq!(gaps.len(), 1);
    assert!(
        reason(&gaps[0]).ends_with("|gap-content:record-marked-stale"),
        "a row that is both stale and missing a required field counts as stale: {}",
        reason(&gaps[0])
    );

    let zero_is_a_reading = read(
        "TimeUS,battery_voltage_v,battery_temperature_k\n0,24.1,295.1\n1000000,24.0,0\n2000000,23.9,0\n3000000,23.8,0\n4000000,23.7,0\n5000000,23.6,295.3\n",
    )
    .expect("reads");
    assert!(
        detect_absences(&p, "bat-01", &zero_is_a_reading, 0, 5_000).is_empty(),
        "a recorded zero is a value; only the operator's units say what it means"
    );

    assert!(
        read("TimeUS,battery_voltage_v,battery_temperature_k\n0,24.1,nan\n").is_err(),
        "a malformed value must not become a missing-field row"
    );
    assert!(read("TimeUS,battery_voltage_v\n0,24.1\nnot-a-row\n").is_err());
}

#[test]
fn the_required_field_declaration_fails_closed() {
    let ask = |body: &str| parse_profile(&format!("{BATTERY_CASE}{body}"), "public");
    let head =
        "\n[[expectations]]\nid = \"x\"\nchannel = \"onboard\"\ncadence_ms = 1000\ngrace_k = 2\n";

    assert!(
        ask(&format!(
            "{head}required_fields = [\"battery_voltage_v\"]\n"
        ))
        .is_ok()
    );

    for bad in [
        "required_fields = []",
        "required_fields = [\"\"]",
        "required_fields = [\"a\",\"a\"]",
        "required_fields = [\" a\"]",
        "required_fields = \"battery_voltage_v\"",
        "required_fields = [1]",
    ] {
        assert!(
            ask(&format!("{head}{bad}\n")).is_err(),
            "must refuse: {bad}"
        );
    }

    let both = ask(&format!(
        "{head}frozen_ms = 2000\nfrozen_fields = [\"battery_voltage_v\"]\nrequired_fields = [\"battery_temperature_k\"]\n"
    ))
    .expect_err("refused");
    assert!(
        both.to_string()
            .contains("both required_fields and frozen_fields"),
        "{both}"
    );

    assert_eq!(
        ask(head).expect("no rule declared").expectations[0].required_fields,
        None
    );
}

#[test]
fn the_shipped_dataflash_profile_is_unchanged_and_answers_the_same_question_at_normal_size() {
    let shipped = parse_profile(ROVER_BIN, "public").expect("shipped profile parses");
    assert_eq!(shipped.expectations.len(), 4);
    assert!(
        shipped
            .expectations
            .iter()
            .all(|e| e.required_fields.is_none()),
        "no shipped expectation declares the new rule, so adopted output is unchanged"
    );
    let rcin = shipped
        .expectations
        .iter()
        .find(|e| e.expectation_id == "rcin_2hz")
        .expect("rcin");
    assert_eq!(rcin.frozen_ms, Some(2_000));

    let p = parse_profile(&format!("{BATTERY_CASE}{ASK}"), "public").expect("profile");
    let mut csv = String::from("TimeUS,battery_voltage_v,battery_temperature_k\n");
    for i in 0..2_400u64 {
        let blank = (1_200..1_210).contains(&i);
        csv.push_str(&format!(
            "{},24.0,{}\n",
            i * 1_000_000,
            if blank { "" } else { "295.0" }
        ));
    }
    let rows = TelemetryCsvReader.read(&p, csv.as_bytes()).expect("reads");
    assert_eq!(rows.len(), 2_400);
    let gaps = detect_absences(&p, "bat-01", &rows, 0, 2_399_000);
    assert_eq!(gaps.len(), 1, "one gap over the blank stretch");
    assert_eq!(gaps[0].absent_since_ms, 1_199_000);
    assert!(
        reason(&gaps[0]).ends_with("|gap-content:required-field-absent"),
        "{}",
        reason(&gaps[0])
    );
}

const ASK_WINDOWED: &str = "\n[[expectations]]\nid = \"battery_1hz\"\nchannel = \"onboard\"\ncadence_ms = 1000\ngrace_k = 2\nrequired_fields = [\"battery_voltage_v\"]\nwindow_rule = \"declared_wall_window\"\n";
const ASK_UNWINDOWED: &str = "\n[[expectations]]\nid = \"battery_1hz\"\nchannel = \"onboard\"\ncadence_ms = 1000\ngrace_k = 2\nrequired_fields = [\"battery_voltage_v\"]\n";

#[test]
fn only_rows_inside_the_declared_window_decide_the_answer_and_outside_rows_survive() {
    let windowed = parse_profile(&format!("{BATTERY_CASE}{ASK_WINDOWED}"), "public").expect("ok");
    let legacy = parse_profile(&format!("{BATTERY_CASE}{ASK_UNWINDOWED}"), "public").expect("ok");
    assert!(windowed.expectations[0].declared_window_only);
    assert!(!legacy.expectations[0].declared_window_only);

    let csv = "TimeUS,battery_voltage_v\n\
               0,24.5\n1000000,24.4\n\
               5000000,24.0\n\
               10000000,23.5\n11000000,23.4\n";
    let rows = TelemetryCsvReader
        .read(&windowed, csv.as_bytes())
        .expect("reads");
    assert_eq!(rows.len(), 5, "every row is read; none is deleted");

    let gaps = detect_absences_with_bounds(&windowed, "bat-01", &rows, 1_500, 8_000);
    assert_eq!(gaps.len(), 2, "one gap before the 5 s row, one after it");

    assert_eq!(gaps[0].0.absent_since_ms, 1_500);
    assert!(
        gaps[0].0.last_good.is_none(),
        "an out-of-window row must never become last_good"
    );
    assert_eq!(gaps[0].1.next_good_ms, Some(5_000));
    assert_eq!(gaps[1].0.absent_since_ms, 5_000);
    assert_eq!(
        gaps[1].1.end,
        musubi_reference_readers::absence::GapEnd::OpenEnd
    );
    assert_eq!(
        gaps[1].1.next_good, None,
        "the 10 s row is outside the window"
    );
    assert_eq!(gaps[1].0.last_good, Some(rows[2].digest));

    let legacy_gaps = detect_absences_with_bounds(&legacy, "bat-01", &rows, 1_500, 8_000);
    assert_eq!(
        legacy_gaps[0].0.absent_since_ms, 1_000,
        "outside row decides"
    );
    assert!(legacy_gaps[0].0.last_good.is_some());
}

#[test]
fn a_window_with_no_rows_reports_one_open_gap_and_borrows_nothing_from_outside() {
    let p = parse_profile(&format!("{BATTERY_CASE}{ASK_WINDOWED}"), "public").expect("ok");
    let rows = TelemetryCsvReader
        .read(
            &p,
            b"TimeUS,battery_voltage_v\n0,24.5\n1000000,24.4\n20000000,23.0\n",
        )
        .expect("reads");
    let gaps = detect_absences_with_bounds(&p, "bat-01", &rows, 5_000, 9_000);
    assert_eq!(gaps.len(), 1);
    assert_eq!(gaps[0].0.absent_since_ms, 5_000);
    assert_eq!(gaps[0].0.expected_by_ms, 7_000);
    assert_eq!(
        gaps[0].0.last_good, None,
        "never-observed inside the window"
    );
    assert_eq!(
        gaps[0].1.end,
        musubi_reference_readers::absence::GapEnd::OpenEnd
    );
    assert!(
        reason(&gaps[0].0).ends_with("|gap-content:no-record-in-gap"),
        "{}",
        reason(&gaps[0].0)
    );
    assert_eq!(rows.len(), 3);
}

#[test]
fn the_window_rule_and_its_channel_are_validated_before_they_are_believed() {
    let ask = |body: &str| parse_profile(&format!("{BATTERY_CASE}{body}"), "public");
    let head =
        "\n[[expectations]]\nid = \"x\"\nchannel = \"onboard\"\ncadence_ms = 1000\ngrace_k = 2\n";

    let unknown = ask(&format!(
        "{head}required_fields = [\"battery_voltage_v\"]\nwindow_rule = \"whenever\"\n"
    ))
    .expect_err("refused");
    assert!(
        unknown.to_string().contains("unknown window_rule"),
        "{unknown}"
    );
    let orphan =
        ask(&format!("{head}window_rule = \"declared_wall_window\"\n")).expect_err("refused");
    assert!(
        orphan.to_string().contains("without required_fields"),
        "{orphan}"
    );

    let typo = "\n[[expectations]]\nid = \"y\"\nchannel = \"gps_ekf\"\ncadence_ms = 1000\ngrace_k = 2\nrequired_fields = [\"battery_voltage_v\"]\n";
    let refused = ask(typo).expect_err("refused");
    assert!(
        refused.to_string().contains("does not declare in channels"),
        "{refused}"
    );
    assert!(
        ask("\n[[expectations]]\nid = \"z\"\nchannel = \"gps_ekf\"\ncadence_ms = 1000\ngrace_k = 2\n")
            .is_ok(),
        "an expectation without required_fields is unaffected"
    );
}
