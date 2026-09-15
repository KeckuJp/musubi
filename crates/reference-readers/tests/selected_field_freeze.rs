use musubi_reference_readers::{
    FieldValue, ProfileReader,
    absence::{apply_frozen_fields_rule, apply_frozen_rule, detect_absences},
    profile::parse_profile,
    telemetry_csv::TelemetryCsvReader,
};
use musubi_reference_types::ChannelId;

fn profile(time: &str, extra: &str) -> String {
    format!(
        r#"profile_id="selected"
version="1"
family="unknown"
source_role="recorded_export"
format="telemetry_csv_us"
extensions=["csv"]
default_clock_basis="unknown"
channels=["onboard"]
[fields]
time="{time}"
[[expectations]]
id="measurement"
channel="onboard"
cadence_ms=1000
grace_k=1
frozen_ms=2000
{extra}
"#
    )
}

#[test]
fn same_selection_works_across_two_source_layouts_without_changing_source_digests() {
    for time in ["TimeUS", "record_time_us"] {
        let p = parse_profile(
            &profile(time, "frozen_fields=[\"battery_voltage_v\"]"),
            "test",
        )
        .unwrap();
        let text = format!(
            "{time},battery_voltage_v,source_record_hex\n0,24,hex:01\n1000000,24,hex:02\n2000000,24,hex:03\n3000000,24,hex:04\n4000000,25,hex:05\n"
        );
        let rows = TelemetryCsvReader.read(&p, text.as_bytes()).unwrap();
        assert!(
            apply_frozen_rule(&rows, ChannelId::Onboard, 2000)
                .iter()
                .all(|o| !o.stale)
        );
        let selected = apply_frozen_fields_rule(
            &rows,
            ChannelId::Onboard,
            2000,
            p.expectations[0].frozen_fields.as_deref(),
        );
        assert_eq!(
            selected.iter().map(|o| o.stale).collect::<Vec<_>>(),
            [false, false, true, true, false]
        );
        for (before, after) in rows.iter().zip(&selected) {
            assert_eq!(before.fields, after.fields);
            assert_eq!(before.digest, after.digest);
        }
        let absences = detect_absences(&p, "single-source", &rows, 0, 4000);
        assert_eq!(absences.len(), 1);
        assert_eq!(absences[0].absent_since_ms, 1000);
        assert_eq!(absences[0].expected_by_ms, 2000);
    }
}

#[test]
fn missing_fields_interrupt_comparison_and_are_not_selected_good_measurements() {
    let p = parse_profile(
        &profile("record_time_us", "frozen_fields=[\"value\"]"),
        "test",
    )
    .unwrap();
    let mut rows = TelemetryCsvReader
        .read(
            &p,
            b"record_time_us,value\n0,1\n1000000,1\n2000000,1\n3000000,1\n4000000,1\n",
        )
        .unwrap();
    rows[1].fields.retain(|(key, _)| key != "value");
    rows[3]
        .fields
        .iter_mut()
        .find(|(key, _)| key == "value")
        .unwrap()
        .1 = FieldValue::Blank;
    let selected =
        apply_frozen_fields_rule(&rows, ChannelId::Onboard, 2000, Some(&["value".into()]));
    assert!(selected.iter().all(|o| !o.stale));
    assert_eq!(
        detect_absences(&p, "single-source", &rows, 0, 4000).len(),
        2
    );
    let mut duplicate = rows[0].clone();
    duplicate.fields.push(("value".into(), FieldValue::I64(1)));
    assert_eq!(
        detect_absences(&p, "single-source", &[duplicate], 0, 4000).len(),
        1
    );
}

#[test]
fn invalid_selections_fail_closed_and_omitted_setting_preserves_default() {
    assert!(
        parse_profile(&profile("t", ""), "test")
            .unwrap()
            .expectations[0]
            .frozen_fields
            .is_none()
    );
    for setting in [
        "frozen_fields=[]",
        "frozen_fields=[\"\"]",
        "frozen_fields=[\"x\",\"x\"]",
        "frozen_fields=[\" x\"]",
    ] {
        assert!(parse_profile(&profile("t", setting), "test").is_err());
    }
    let no_threshold = profile("t", "frozen_fields=[\"x\"]").replace("frozen_ms=2000", "");
    assert!(parse_profile(&no_threshold, "test").is_err());
}
