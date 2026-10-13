use musubi_reference_readers::{FieldValue, Observation, bin::ArduPilotBinReader, field};

fn fmt(id: u8, name: &str, format: &str, labels: &str, payload: u8) -> Vec<u8> {
    let mut out = vec![0xa3, 0x95, 128, id, payload + 3];
    for (text, size) in [(name, 4), (format, 16), (labels, 64)] {
        let mut bytes = vec![0; size];
        bytes[..text.len()].copy_from_slice(text.as_bytes());
        out.extend(bytes);
    }
    out
}
fn record(id: u8, tail: &[u8]) -> Vec<u8> {
    let mut out = vec![0xa3, 0x95, id];
    out.extend(1000_u64.to_le_bytes());
    out.extend(tail);
    out
}
fn setup() -> Vec<u8> {
    let mut out = fmt(130, "UNIT", "QbZ", "TimeUS,Id,Label", 73);
    out.extend(fmt(131, "MULT", "Qbd", "TimeUS,Id,Mult", 17));
    out.extend(fmt(
        132,
        "FMTU",
        "QBNN",
        "TimeUS,FmtType,UnitIds,MultIds",
        41,
    ));
    out
}
fn unit(id: u8, label: &str) -> Vec<u8> {
    let mut tail = vec![id];
    let mut text = [0; 64];
    text[..label.len()].copy_from_slice(label.as_bytes());
    tail.extend(text);
    record(130, &tail)
}
fn multiplier(id: u8, value: f64) -> Vec<u8> {
    let mut tail = vec![id];
    tail.extend(value.to_le_bytes());
    record(131, &tail)
}
fn binding(id: u8, units: &str, mults: &str) -> Vec<u8> {
    let mut tail = vec![id];
    for text in [units, mults] {
        let mut bytes = [0; 16];
        bytes[..text.len()].copy_from_slice(text.as_bytes());
        tail.extend(bytes);
    }
    record(132, &tail)
}
fn profile() -> musubi_reference_readers::FamilyProfile {
    musubi_reference_readers::profile::parse_profile(
        include_str!("../../../profiles/public/ardupilot_plane_bin.toml"),
        "test",
    )
    .unwrap()
}
fn value(row: &Observation, name: &str) -> FieldValue {
    field(row, name).unwrap().clone()
}

#[test]
fn raw_centi_and_already_float_values_scale_once_at_common_output() {
    let mut input = setup();
    input.extend(fmt(140, "TEST", "QcCf", "TimeUS,V,I,F", 16));
    input.extend(unit(b'v', "V"));
    input.extend(unit(b'A', "A"));
    input.extend(multiplier(b'B', 0.01));
    input.extend(multiplier(b'0', 1.0));
    input.extend(binding(140, "svAv", "FBB0"));
    let mut tail = 1234_i16.to_le_bytes().to_vec();
    tail.extend(567_u16.to_le_bytes());
    tail.extend(12.5_f32.to_le_bytes());
    input.extend(record(140, &tail));
    let reader = ArduPilotBinReader;
    let old = reader.read_report(&profile(), &input).unwrap();
    let report = reader.read_report_with_units(&profile(), &input).unwrap();
    assert_eq!(report.data_records, old.data_records);
    assert_eq!(report.observations.len(), old.observations.len());
    for (original, decorated) in old.observations.iter().zip(&report.observations) {
        assert_eq!(original.t_boot_us, decorated.t_boot_us);
        assert_eq!(original.clock_basis, decorated.clock_basis);
        for (name, raw) in &original.fields {
            assert_eq!(field(decorated, name), Some(raw));
        }
    }
    let row = report.observations.last().unwrap();
    assert_eq!(value(row, "TEST.V"), FieldValue::I64(1234));
    assert_eq!(
        value(row, "TEST.V.reported_unit_value"),
        FieldValue::F64(12.34)
    );
    assert_eq!(
        value(row, "TEST.I.reported_unit_value"),
        FieldValue::F64(5.67)
    );
    assert_eq!(
        value(row, "TEST.F.reported_unit_value"),
        FieldValue::F64(12.5)
    );
    assert_eq!(row.t_boot_us, Some(1000));
    assert!(field(row, "TEST.TimeUS.reported_unit_value").is_none());
    assert!(
        field(
            old.observations.last().unwrap(),
            "TEST.V.reported_unit_value"
        )
        .is_none()
    );
}

#[test]
fn metadata_order_updates_and_reused_type_are_not_retroactive() {
    let mut input = setup();
    input.extend(fmt(140, "DIST", "Qe", "TimeUS,X", 12));
    input.extend(record(140, &1200_i32.to_le_bytes()));
    input.extend(unit(b'm', "m"));
    input.extend(multiplier(b'C', 0.001));
    input.extend(binding(140, "sm", "FC"));
    input.extend(record(140, &1200_i32.to_le_bytes()));
    input.extend(multiplier(b'C', 0.002));
    input.extend(record(140, &1200_i32.to_le_bytes()));
    input.extend(fmt(140, "NEXT", "QE", "TimeUS,Y", 12));
    input.extend(record(140, &1200_u32.to_le_bytes()));
    input.extend(binding(140, "sm", "FC"));
    input.extend(record(140, &1200_u32.to_le_bytes()));
    let report = ArduPilotBinReader
        .read_report_with_units(&profile(), &input)
        .unwrap();
    let rows: Vec<_> = report
        .observations
        .iter()
        .filter(|o| field(o, "DIST.X").is_some() || field(o, "NEXT.Y").is_some())
        .collect();
    assert_eq!(rows.len(), 5);
    assert_eq!(
        value(rows[0], "DIST.X.reported_unit_disposition"),
        FieldValue::Text("FMTU_NOT_AVAILABLE".into())
    );
    assert_eq!(
        value(rows[1], "DIST.X.reported_unit_value"),
        FieldValue::F64(1.2)
    );
    assert_eq!(
        value(rows[2], "DIST.X.reported_unit_value"),
        FieldValue::F64(2.4)
    );
    assert_eq!(
        value(rows[3], "NEXT.Y.reported_unit_value"),
        FieldValue::Blank
    );
    assert_eq!(
        value(rows[4], "NEXT.Y.reported_unit_value"),
        FieldValue::F64(2.4)
    );
    assert!(rows.iter().all(|o| o.t_boot_us == Some(1000)));
}

#[test]
fn unknown_nonfinite_large_integer_and_untimed_data_remain_visible() {
    let mut input = setup();
    input.extend(fmt(140, "RAW", "QdfqL", "TimeUS,D,F,N,L", 32));
    input.extend(unit(b'm', "m"));
    input.extend(unit(b'?', "UNKNOWN"));
    input.extend(multiplier(b'0', 1.0));
    input.extend(multiplier(b'?', 1.0));
    input.extend(binding(140, "smmm?", "F0000"));
    let mut tail = f64::INFINITY.to_le_bytes().to_vec();
    tail.extend(f32::NAN.to_le_bytes());
    tail.extend((9_007_199_254_740_993_i64).to_le_bytes());
    tail.extend(100_i32.to_le_bytes());
    input.extend(record(140, &tail));
    input.extend(fmt(141, "NOCL", "h", "X", 2));
    input.extend(binding(141, "m", "?"));
    input.extend([0xa3, 0x95, 141, 10, 0]);
    let report = ArduPilotBinReader
        .read_report_with_units(&profile(), &input)
        .unwrap();
    let row = report
        .observations
        .iter()
        .find(|o| field(o, "RAW.D").is_some())
        .unwrap();
    for name in ["D", "F", "N", "L"] {
        assert_eq!(
            value(row, &format!("RAW.{name}.reported_unit_value")),
            FieldValue::Blank
        );
    }
    assert!(matches!(value(row, "RAW.D"), FieldValue::Text(_)));
    assert_eq!(value(row, "RAW.N"), FieldValue::I64(9_007_199_254_740_993));
    assert_eq!(
        value(row, "RAW.L.reported_unit_disposition"),
        FieldValue::Text("UNIT_UNQUALIFIED".into())
    );
    assert_eq!(report.untimed_records.len(), 1);
    let fields = &report.untimed_records[0].fields;
    assert!(
        fields
            .iter()
            .any(|(k, v)| k == "NOCL.X.reported_unit_disposition"
                && *v == FieldValue::Text("MULTIPLIER_UNQUALIFIED".into()))
    );
    assert_eq!(report.untimed_records[0].bytes, [0xa3, 0x95, 141, 10, 0]);
}

#[test]
fn malformed_metadata_and_derived_name_collisions_refuse_opt_in_only() {
    let mut wrong_length = setup();
    wrong_length.extend(fmt(140, "TEST", "Qh", "TimeUS,X", 10));
    wrong_length.extend(binding(140, "m", "C"));
    assert!(
        ArduPilotBinReader
            .read_report(&profile(), &wrong_length)
            .is_ok()
    );
    assert!(
        ArduPilotBinReader
            .read_report_with_units(&profile(), &wrong_length)
            .is_err()
    );
    let mut missing = setup();
    missing.extend(binding(144, "m", "C"));
    assert!(
        ArduPilotBinReader
            .read_report_with_units(&profile(), &missing)
            .is_err()
    );
    let mut wrong_shape = fmt(130, "UNIT", "QHZ", "TimeUS,Id,Label", 74);
    wrong_shape.extend(record(130, &[0; 66]));
    assert!(
        ArduPilotBinReader
            .read_report(&profile(), &wrong_shape)
            .is_ok()
    );
    assert!(
        ArduPilotBinReader
            .read_report_with_units(&profile(), &wrong_shape)
            .is_err()
    );
    let mut collision = fmt(140, "TEST", "Qhh", "TimeUS,X,X.reported_unit_value", 12);
    collision.extend(record(140, &[0; 4]));
    assert!(
        ArduPilotBinReader
            .read_report(&profile(), &collision)
            .is_ok()
    );
    assert!(
        ArduPilotBinReader
            .read_report_with_units(&profile(), &collision)
            .is_err()
    );
}

#[test]
fn remaining_finite_units_and_float_range_do_not_invent_values() {
    let mut input = setup();
    input.extend(fmt(140, "MEAS", "QLdf", "TimeUS,Len,Rate,Duration", 24));
    for (id, label) in [(b'm', "m"), (b'E', "rad/s"), (b's', "s"), (b'o', "m/s/s")] {
        input.extend(unit(id, label));
    }
    input.extend(multiplier(b'C', 0.001));
    input.extend(multiplier(b'0', 1.0));
    input.extend(binding(140, "smEs", "FC00"));
    let mut tail = (-1000_i32).to_le_bytes().to_vec();
    tail.extend(0.125_f64.to_le_bytes());
    tail.extend(2.5_f32.to_le_bytes());
    input.extend(record(140, &tail));
    input.extend(fmt(141, "ACC", "Qd", "TimeUS,A", 16));
    input.extend(binding(141, "so", "F0"));
    input.extend(record(141, &9.8_f64.to_le_bytes()));
    input.extend(multiplier(b'0', f64::MAX));
    input.extend(record(141, &2.0_f64.to_le_bytes()));
    input.extend(multiplier(b'0', f64::MIN_POSITIVE));
    input.extend(record(141, &f64::MIN_POSITIVE.to_le_bytes()));
    let report = ArduPilotBinReader
        .read_report_with_units(&profile(), &input)
        .unwrap();
    let row = report
        .observations
        .iter()
        .find(|o| field(o, "MEAS.Len").is_some())
        .unwrap();
    assert_eq!(
        value(row, "MEAS.Len.reported_unit_value"),
        FieldValue::F64(-1.0)
    );
    assert_eq!(
        value(row, "MEAS.Rate.reported_unit_value"),
        FieldValue::F64(0.125)
    );
    assert_eq!(
        value(row, "MEAS.Duration.reported_unit_value"),
        FieldValue::F64(2.5)
    );
    let rows: Vec<_> = report
        .observations
        .iter()
        .filter(|o| field(o, "ACC.A").is_some())
        .collect();
    assert_eq!(
        value(rows[0], "ACC.A.reported_unit_value"),
        FieldValue::F64(9.8)
    );
    assert_eq!(
        value(rows[1], "ACC.A.reported_unit_disposition"),
        FieldValue::Text("DERIVED_OVERFLOW".into())
    );
    assert_eq!(
        value(rows[2], "ACC.A.reported_unit_disposition"),
        FieldValue::Text("DERIVED_UNDERFLOW".into())
    );
    assert_eq!(
        value(rows[2], "ACC.A.reported_unit_value"),
        FieldValue::Blank
    );
}
