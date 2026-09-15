use musubi_reference_readers::{
    ClockBasis, FamilyProfile, FieldValue, field, mavlog_json::MavlogJsonReader,
    profile::parse_profile,
};
use musubi_types::PlatformDomain;
use serde_json::{Value, json};

fn profile(family: &str) -> FamilyProfile {
    musubi_reference_readers::profile::parse_profile(
        &format!(
            r#"
profile_id = "independent_contract"
version = "1"
family = "{family}"
source_role = "fc"
format = "pymavlink_dataflash_jsonl"
extensions = ["jsonl"]
default_clock_basis = "gps_locked"
[fields]
time = "TimeUS"
"#
        ),
        "public",
    )
    .expect("synthetic profile")
}

fn bytes(records: &[Value]) -> Vec<u8> {
    records
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n")
        .into_bytes()
}

fn record(name: &str, data: Value) -> Value {
    json!({"meta": {"type": name, "timestamp": 1777777777.125}, "data": data})
}

#[test]
fn fixed_ardu_vehicle_imu_profiles_reuse_values_without_domain_inference() {
    let unknown = parse_profile(
        include_str!("fixtures/ardupilot-mode--imu-unclassified-profile.toml"),
        "test",
    )
    .unwrap();
    let rover = parse_profile(
        include_str!("fixtures/ardupilot-mode--imu-rover-profile.toml"),
        "test",
    )
    .unwrap();
    assert_eq!(unknown.field_units, rover.field_units);
    for (identity, p, domain) in [
        ("ArduCopter 4.5.5", &unknown, PlatformDomain::Unknown),
        ("ArduSub 4.5.7", &unknown, PlatformDomain::Unknown),
        ("Rover 4.5.5", &rover, PlatformDomain::Ground),
    ] {
        let mut input = vec![
            record("MSG", json!({"Message":identity})),
            record(
                "IMU",
                json!({"TimeUS":1234567,"I":0,"GyrX":-0.25,"GyrY":0.5,
                "GyrZ":1.25,"AccX":9.8125,"AccY":-2.0,"AccZ":0.0}),
            ),
            record(
                "IMU",
                json!({"TimeUS":1234567,"I":1,"GyrX":0.75,"GyrY":-0.5,
                "GyrZ":2.5,"AccX":9.5,"AccY":2.0,"AccZ":0.125,"Future":{"unknown":true}}),
            ),
        ];
        if identity.starts_with("Rover") {
            input.push(record("PARM", json!({"Name":"FRAME_CLASS","Value":1})));
        }
        let report = MavlogJsonReader.read_report(p, &bytes(&input)).unwrap();
        assert_eq!(report.records, input);
        assert_eq!(report.platform_domain, domain);
        assert_eq!(report.observations.len(), 2);
        assert_eq!(report.untimed_records, input.len() - 2);
        for (index, expected) in [-0.25, 0.75].iter().enumerate() {
            let row = &report.observations[index];
            assert_eq!(field(row, "IMU.I"), Some(&FieldValue::I64(index as i64)));
            assert_eq!(field(row, "IMU.GyrX"), Some(&FieldValue::F64(*expected)));
            assert_eq!(row.t_boot_us, Some(1234567));
            assert_eq!(row.clock_basis, ClockBasis::BootRelative);
            assert!(row.anchor_unix_us.is_none());
        }
        input[1]["data"]["TimeUS"] = json!(-1);
        assert!(MavlogJsonReader.read_report(p, &bytes(&input)).is_err());
    }
    let contradictory = [
        record("MSG", json!({"Message":"Rover 4.5.5"})),
        record("IMU", json!({"TimeUS":1000,"I":0,"GyrX":0.5})),
    ];
    assert!(
        MavlogJsonReader
            .read_report(&unknown, &bytes(&contradictory))
            .is_err()
    );
}

#[test]
fn selected_plane_4x_instances_reuse_imu_and_attitude_profile() {
    let p = parse_profile(
        include_str!("fixtures/ardupilot-mode--imu-profile.toml"),
        "public",
    )
    .unwrap();
    assert_eq!(
        p.field_units.get("IMU.I").map(String::as_str),
        Some("instance")
    );
    for version in ["4.2.3", "4.3.1"] {
        let input = [
            record("MSG", json!({"Message":format!("ArduPlane {version}")})),
            record(
                "IMU",
                json!({"TimeUS":1234567,"I":0,"GyrX":-0.25,"GyrY":0.5,
                "GyrZ":1.25,"AccX":9.8125,"AccY":-2.0,"AccZ":0.0}),
            ),
            record(
                "IMU",
                json!({"TimeUS":1234567,"I":1,"GyrX":0.75,"GyrY":-0.5,
                "GyrZ":2.5,"AccX":9.5,"AccY":2.0,"AccZ":0.125,"Future":null}),
            ),
            record(
                "ATT",
                json!({"TimeUS":1234567,"DesRoll":10.25,"Roll":-20.5,
                "DesPitch":30.75,"Pitch":-40.0,"DesYaw":123.45,"Yaw":359.99,"AEKF":3}),
            ),
        ];
        let report = MavlogJsonReader.read_report(&p, &bytes(&input)).unwrap();
        assert_eq!(report.records, input);
        assert_eq!(report.untimed_records, 1);
        assert_eq!(report.observations.len(), 3);
        assert_eq!(report.platform_domain, PlatformDomain::Air);
        for (index, instance) in [0, 1].iter().enumerate() {
            assert_eq!(
                field(&report.observations[index], "IMU.I"),
                Some(&FieldValue::I64(*instance))
            );
        }
        for observation in &report.observations {
            assert_eq!(observation.t_boot_us, Some(1234567));
            assert_eq!(observation.anchor_unix_us, None);
        }
        assert_eq!(
            field(&report.observations[0], "IMU.GyrX"),
            Some(&FieldValue::F64(-0.25))
        );
        assert_eq!(
            field(&report.observations[1], "IMU.Future"),
            Some(&FieldValue::Blank)
        );
        assert_eq!(
            field(&report.observations[2], "ATT.Yaw"),
            Some(&FieldValue::F64(359.99))
        );
        assert_eq!(
            field(&report.observations[2], "ATT.AEKF"),
            Some(&FieldValue::I64(3))
        );
    }
}

#[test]
fn sensor_local_timems_is_retained_without_boot_clock_promotion() {
    let input = [
        record("MSG", json!({"TimeUS": 1000, "Message": "ArduPlane 4.6.0"})),
        record("ACC1", json!({"TimeMS": 2, "AccX": 1.25})),
        record("GYR", json!({"TimeMS": 3, "GyrX": 0.5})),
    ];
    let report = MavlogJsonReader
        .read_report(&profile("fixed_wing"), &bytes(&input))
        .expect("sensor-local clocks remain retained records");
    assert_eq!(report.records, input);
    assert_eq!(report.observations.len(), 1);
    assert_eq!(report.observations[0].t_boot_us, Some(1000));
    assert_eq!(report.untimed_records, 2);

    let acc_with_boot = [record("ACC1", json!({"TimeUS": 4000, "AccX": 1.25}))];
    let report = MavlogJsonReader
        .read_report(&profile("fixed_wing"), &bytes(&acc_with_boot))
        .expect("explicit sensor TimeUS remains a boot observation");
    assert_eq!(report.records, acc_with_boot);
    assert_eq!(report.observations.len(), 1);
    assert_eq!(report.observations[0].t_boot_us, Some(4000));
    assert_eq!(report.untimed_records, 0);
}

#[test]
fn fixed_wing_preserves_changing_frame_class_without_rover_rejection() {
    let input = [
        record("MSG", json!({"Message": "ArduPlane 4.6.0"})),
        record(
            "PARM",
            json!({"TimeUS": 1000, "Name": "FRAME_CLASS", "Value": 1}),
        ),
        record(
            "PARM",
            json!({"TimeUS": 2000, "Name": "FRAME_CLASS", "Value": 2}),
        ),
    ];
    let report = MavlogJsonReader
        .read_report(&profile("fixed_wing"), &bytes(&input))
        .expect("Plane parameters do not inherit Rover frame restrictions");
    assert_eq!(report.records, input);
    assert_eq!(report.observations.len(), 2);
    assert_eq!(report.platform_domain, PlatformDomain::Air);
}

#[test]
fn decimal_export_values_preserve_exact_f64_bits() {
    let input = br#"{"meta":{"type":"GPS","timestamp":1509241629.8634121},"data":{"TimeUS":1234567,"Lat":35.123456789012345,"Lng":139.98765432109876,"Alt":123.45678901234567}}"#;
    let report = MavlogJsonReader
        .read_report(&profile("fixed_wing"), input)
        .expect("decimal export");
    assert_eq!(
        report.records[0]["meta"]["timestamp"]
            .as_f64()
            .unwrap()
            .to_bits(),
        "1509241629.8634121".parse::<f64>().unwrap().to_bits()
    );
    for (name, decimal) in [
        ("Lat", "35.123456789012345"),
        ("Lng", "139.98765432109876"),
        ("Alt", "123.45678901234567"),
    ] {
        let expected = decimal.parse::<f64>().unwrap().to_bits();
        assert_eq!(
            report.records[0]["data"][name].as_f64().unwrap().to_bits(),
            expected
        );
        let field_name = format!("GPS.{name}");
        let value = &report.observations[0]
            .fields
            .iter()
            .find(|(key, _)| key == &field_name)
            .unwrap()
            .1;
        assert_eq!(value.as_f64().unwrap().to_bits(), expected);
    }
}

#[test]
fn decoded_units_and_unknown_values_survive_without_wall_clock_promotion() {
    let metadata = record("FMT", json!({"Name":"GPS", "Columns":"TimeUS,Lat,Lng"}));
    let gps = record(
        "GPS",
        json!({"TimeUS":1234567, "Lat":35.125, "Lng":139.875,
        "Alt":72.25, "Spd":3.5, "Extra":{"array":[true,null,4]}, "Huge":18446744073709551615_u64}),
    );
    let input = [
        metadata.clone(),
        gps.clone(),
        record("BAT", json!({"TimeMS":1250,"Volt":12.6})),
    ];
    let report = MavlogJsonReader
        .read_report(&profile("fixed_wing"), &bytes(&input))
        .expect("decoded records");
    assert_eq!(report.records, input);
    assert_eq!(report.untimed_records, 1);
    assert_eq!(report.observations.len(), 2);
    let observation = &report.observations[0];
    for (name, expected) in [
        ("GPS.Lat", 35.125),
        ("GPS.Lng", 139.875),
        ("GPS.Alt", 72.25),
        ("GPS.Spd", 3.5),
    ] {
        assert_eq!(
            observation
                .fields
                .iter()
                .find(|(key, _)| key == name)
                .unwrap()
                .1
                .as_f64(),
            Some(expected)
        );
    }
    assert_eq!(observation.t_boot_us, Some(1234567));
    assert_eq!(observation.t_ms, 1234);
    assert_eq!(report.observations[1].t_boot_us, Some(1250000));
    assert_eq!(
        report.observations[1]
            .fields
            .iter()
            .find(|(key, _)| key == "BAT.Volt")
            .unwrap()
            .1
            .as_f64(),
        Some(12.6)
    );
    assert_eq!(
        observation
            .fields
            .iter()
            .find(|(key, _)| key == "GPS.Huge")
            .unwrap()
            .1,
        FieldValue::Text("18446744073709551615".into())
    );
    for observation in &report.observations {
        assert_eq!(observation.clock_basis, ClockBasis::BootRelative);
        assert_eq!(observation.wall_ms, None);
        assert_eq!(observation.anchor_unix_us, None);
    }
    assert_eq!(report.platform_domain, PlatformDomain::Unknown);
}

#[test]
fn firmware_versions_and_added_fields_do_not_require_a_new_parser() {
    for version in ["ArduPlane 4.4.4", "ArduPlane 4.6.0-dev abc123"] {
        let input = [
            record("MSG", json!({"Message":version})),
            record("NEWMSG", json!({"TimeUS":42,"FutureField":[1,"new",false]})),
        ];
        let report = MavlogJsonReader
            .read_report(&profile("fixed_wing"), &bytes(&input))
            .unwrap();
        assert_eq!(report.platform_domain, PlatformDomain::Air);
        assert_eq!(report.firmware_identity.as_deref(), Some(version));
        assert_eq!(report.records, input);
        assert!(
            report.observations[0]
                .fields
                .iter()
                .any(|(name, _)| name == "NEWMSG.FutureField")
        );
    }
}

#[test]
fn rover_domain_requires_in_log_identity_and_frame() {
    for (identity, frame, expected) in [
        (Some("Rover 4.5.1"), Some(1), PlatformDomain::Ground),
        (Some("ArduRover 4.6.0"), Some(2), PlatformDomain::Surface),
        (Some("Rover 4.5.1"), None, PlatformDomain::Unknown),
        (None, Some(1), PlatformDomain::Unknown),
        (None, None, PlatformDomain::Unknown),
    ] {
        let mut input = vec![];
        if let Some(identity) = identity {
            input.push(record("MSG", json!({"Message":identity})));
        }
        if let Some(frame) = frame {
            input.push(record("PARM", json!({"Name":"FRAME_CLASS","Value":frame})));
        }
        input.push(record("GPS", json!({"TimeUS":1000})));
        let report = MavlogJsonReader
            .read_report(&profile("ugv"), &bytes(&input))
            .unwrap();
        assert_eq!(report.platform_domain, expected);
    }
}

#[test]
fn contradictory_identity_and_wrong_format_are_rejected() {
    for (family, firmware) in [("ugv", "ArduPlane 4.5.0"), ("fixed_wing", "Rover 4.5.0")] {
        let input = [
            record("MSG", json!({"Message":firmware})),
            record("GPS", json!({"TimeUS":1})),
        ];
        assert!(
            MavlogJsonReader
                .read_report(&profile(family), &bytes(&input))
                .is_err()
        );
    }
    let mut wrong = profile("ugv");
    wrong.format = "ardupilot_dataflash_bin".into();
    assert!(
        MavlogJsonReader
            .read_report(&wrong, &bytes(&[record("GPS", json!({"TimeUS":1}))]))
            .is_err()
    );
}

#[test]
fn invalid_time_fields_and_malformed_records_fail_without_partial_success() {
    for data in [
        json!({"TimeUS":-1}),
        json!({"TimeUS":1.5}),
        json!({"TimeUS":"1000"}),
        json!({"TimeUS":null}),
        json!({"TimeMS":18446744073709551615_u64}),
        json!({"TimeMS":1,"TimeUS":2000}),
    ] {
        assert!(
            MavlogJsonReader
                .read_report(
                    &profile("ugv"),
                    &bytes(&[record("GPS", json!({"TimeUS":1})), record("GPS", data)])
                )
                .is_err()
        );
    }
    for malformed in [
        b"".as_slice(),
        b"\xff",
        b"not json",
        b"[]",
        b"{}",
        b"{\"meta\":{\"type\":\"GPS\"},\"data\":[]}",
        b"{\"meta\":{\"type\":\"\"},\"data\":{\"TimeUS\":1}}",
    ] {
        assert!(
            MavlogJsonReader
                .read_report(&profile("ugv"), malformed)
                .is_err()
        );
    }
    assert!(
        MavlogJsonReader
            .read_report(
                &profile("ugv"),
                &bytes(&[record("MSG", json!({"Message":"metadata only"}))])
            )
            .is_err()
    );
}
#[test]
fn fixed_imu_profile_reuses_reader_without_rescaling_or_merging_instances() {
    let p = parse_profile(
        include_str!("fixtures/ardupilot-mode--imu-profile.toml"),
        "test",
    )
    .unwrap();
    let input = bytes(&[
        record("FMT", json!({"Name":"IMU"})),
        record(
            "IMU",
            json!({"TimeUS":1000,"GyrX":0.25,"AccZ":-9.81,"T":0,"future":7}),
        ),
        record("IMU2", json!({"TimeUS":1000,"GyrX":0.5,"AccZ":-9.8,"T":0})),
        record("IMU3", json!({"TimeUS":1001,"GyrX":0.75,"AccZ":-9.7,"T":0})),
    ]);
    let report = MavlogJsonReader.read_report(&p, &input).unwrap();
    assert_eq!(
        (
            report.records.len(),
            report.observations.len(),
            report.untimed_records
        ),
        (4, 3, 1)
    );
    for (index, name) in ["IMU", "IMU2", "IMU3"].iter().enumerate() {
        assert_eq!(p.field_units[&format!("{name}.GyrX")], "rad/s");
        assert_eq!(p.field_units[&format!("{name}.AccZ")], "m/s2");
        assert_eq!(
            field(&report.observations[index], &format!("{name}.GyrX")),
            Some(&FieldValue::F64((index + 1) as f64 * 0.25))
        );
        assert!(report.observations[index].anchor_unix_us.is_none());
    }
    assert!(!p.field_units.contains_key("IMU.T"));
    assert_eq!(
        field(&report.observations[0], "IMU.future"),
        Some(&FieldValue::I64(7))
    );
}

#[test]
fn reported_attitude_and_targets_remain_distinct_decoded_degrees() {
    let p = parse_profile(
        include_str!("fixtures/ardupilot-mode--imu-profile.toml"),
        "test",
    )
    .unwrap();
    let names = ["Roll", "Pitch", "Yaw", "DesRoll", "DesPitch", "DesYaw"];
    let values = [-7.18, 2.58, 337.1, -0.44, -2.38, 0.0];
    let mut data = serde_json::Map::new();
    data.insert("TimeUS".into(), json!(81795644));
    data.insert("unknown".into(), json!({"kept":true}));
    for (name, value) in names.iter().zip(values) {
        data.insert((*name).into(), json!(value));
    }
    let report = MavlogJsonReader
        .read_report(&p, &bytes(&[record("ATT", Value::Object(data))]))
        .unwrap();
    assert_eq!(report.observations.len(), 1);
    let o = &report.observations[0];
    assert_eq!(o.t_boot_us, Some(81795644));
    assert!(o.anchor_unix_us.is_none());
    for (name, value) in names.iter().zip(values) {
        assert_eq!(p.field_units[&format!("ATT.{name}")], "deg");
        assert_eq!(
            field(o, &format!("ATT.{name}")),
            Some(&FieldValue::F64(value))
        );
    }
    assert!(field(o, "ATT.unknown").is_some());
    assert!(!p.field_units.contains_key("ATT.ErrRP"));
}
