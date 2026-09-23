use musubi_reference_readers::{
    FieldValue, field, profile::parse_profile, telemetry_csv::TelemetryCsvReader,
};
use musubi_reference_types::ClockBasis;
use std::io::Write;
use std::process::{Command, Stdio};

fn converted(format: &str, input: &str) -> Vec<u8> {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut child = Command::new("python3")
        .current_dir(root)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .args(["-c", "import sys; from scripts.convert_ardupilot_battery_csv import convert; sys.stdout.write(convert(sys.stdin.read(), sys.argv[1]))", format])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    let result = child.wait_with_output().unwrap();
    assert!(result.status.success(), "{:?}", result.stderr);
    result.stdout
}

#[test]
fn ros_and_both_vda_layouts_use_the_same_reader_without_clock_or_domain_inference() {
    let profile = parse_profile(
        include_str!("fixtures/unknown-adapter--electrical-schema-reuse--json-profile.toml"),
        "test",
    )
    .unwrap();
    let cases = [
        (
            "ros-battery-json",
            r#"{"header":{"stamp":{"sec":2,"nanosec":123456789},"frame_id":"pack"},"voltage":24,"current":-2,"charge":3,"percentage":0.75,"power_supply_status":2,"present":true,"unknown":{"nested":1}}"#,
            Some(-48.),
        ),
        (
            "vda-state-2.1",
            r#"{"version":"2.1.0","timestamp":"2026-01-01T00:00:00.123Z","manufacturer":"synthetic","serialNumber":"1","batteryState":{"batteryCharge":75,"batteryVoltage":24,"charging":false},"unknown":1}"#,
            None,
        ),
        (
            "vda-state-3.0",
            r#"{"version":"3.0.0","timestamp":"2026-01-01T00:00:00.123Z","manufacturer":"synthetic","serialNumber":"1","powerSupply":{"stateOfCharge":75,"batteryVoltage":24,"batteryCurrent":2,"charging":false},"unknown":1}"#,
            Some(48.),
        ),
    ];
    for (format, input, power) in cases {
        let bytes = converted(format, &format!("{input}\n{input}\n"));
        let observations = TelemetryCsvReader
            .read_non_decreasing(&profile, &bytes)
            .unwrap();
        assert_eq!(observations.len(), 2);
        for observation in observations {
            assert_eq!(observation.clock_basis, ClockBasis::Unknown);
            assert_eq!(
                (
                    observation.t_boot_us,
                    observation.wall_ms,
                    observation.anchor_unix_us
                ),
                (None, None, None)
            );
            assert_eq!(
                field(&observation, "battery_voltage_v").unwrap().as_f64(),
                Some(24.)
            );
            assert_eq!(
                field(&observation, "battery_remaining_fraction")
                    .unwrap()
                    .as_f64(),
                Some(0.75)
            );
            assert_eq!(
                field(&observation, "battery_power_w").unwrap().as_f64(),
                power
            );
            let expected = format!(
                "hex:{}",
                input
                    .as_bytes()
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            );
            assert_eq!(
                field(&observation, "source_record_hex"),
                Some(&FieldValue::Text(expected))
            );
        }
    }
}

#[test]
fn rover_alias_and_unavailable_current_reuse_the_adopted_bat_profile() {
    let profile = parse_profile(
        include_str!("fixtures/unknown-adapter--ardupilot-battery--profile.toml"),
        "test",
    )
    .unwrap();
    let input = "TimeUS,Inst,Volt,Curr,CurrTot,EnrgTot,RemPct\n1000,1,24,nan,nan,nan,255\n";
    let bytes = converted("ardupilot-bat-inst", input);
    let observations = TelemetryCsvReader
        .read_with_options(
            &profile,
            &bytes,
            musubi_decoded_csv::ParseOptions {
                allow_equal_time: true,
                preserve_nonfinite_as_text: true,
            },
        )
        .unwrap();
    assert_eq!(observations.len(), 1);
    assert_eq!(field(&observations[0], "Inst"), Some(&FieldValue::I64(1)));
    assert_eq!(
        field(&observations[0], "Curr"),
        Some(&FieldValue::Text("nan".into()))
    );
    assert_eq!(
        field(&observations[0], "battery_current_a"),
        Some(&FieldValue::Blank)
    );
    assert_eq!(observations[0].t_boot_us, Some(1000));
}

#[test]
fn vda_reported_state_and_unknown_enum_reach_common_fields_without_control_inference() {
    let profile = parse_profile(
        include_str!("fixtures/unknown-adapter--electrical-schema-reuse--json-profile.toml"),
        "test",
    )
    .unwrap();
    for (format, version, battery, safety, mode) in [
        (
            "vda-state-2.1",
            "2.1.0",
            r#""batteryState":{"batteryCharge":50,"charging":false}"#,
            "eStop",
            "TEACHIN",
        ),
        (
            "vda-state-3.0",
            "3.0.0",
            r#""powerSupply":{"stateOfCharge":50,"charging":false}"#,
            "activeEmergencyStop",
            "TEACH_IN",
        ),
    ] {
        let input = format!(
            r#"{{"version":"{version}","timestamp":"2026-01-01T00:00:00Z","manufacturer":"synthetic","serialNumber":"1",{battery},"operatingMode":"{mode}","driving":true,"velocity":{{"vx":-2}},"safetyState":{{"{safety}":"FUTURE","fieldViolation":false}},"errors":[]}}"#
        );
        let bytes = converted(format, &input);
        let rows = TelemetryCsvReader
            .read_non_decreasing(&profile, &bytes)
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(
            field(&rows[0], "vda_operating_mode_reported"),
            Some(&FieldValue::Text("TEACH_IN".into()))
        );
        assert_eq!(
            field(&rows[0], "vda_emergency_stop_reported"),
            Some(&FieldValue::Text("UNKNOWN".into()))
        );
        assert_eq!(
            field(&rows[0], "vehicle_vx_m_s").unwrap().as_f64(),
            Some(-2.)
        );
        assert_eq!(
            field(&rows[0], "vda_error_fatal_count").unwrap().as_f64(),
            Some(0.)
        );
        assert_eq!(rows[0].clock_basis, ClockBasis::Unknown);
        assert_eq!(rows[0].t_boot_us, None);
    }
}
