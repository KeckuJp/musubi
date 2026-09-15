use musubi_decoded_csv::ParseOptions;
use musubi_reference_readers::{
    ClockBasis, FieldValue, profile::parse_profile, telemetry_csv::TelemetryCsvReader,
};
use musubi_reference_types::Family;
use musubi_types::PlatformDomain;

const USV: &str = include_str!("fixtures/surface-usv--profile.toml");
const SURVEY: &str = include_str!("fixtures/surface-usv--survey-boat-profile.toml");
const ROV: &str = include_str!("fixtures/surface-usv--rov-profile.toml");

fn options() -> ParseOptions {
    ParseOptions {
        allow_equal_time: true,
        ..ParseOptions::default()
    }
}

fn field<'a>(o: &'a musubi_reference_readers::Observation, key: &str) -> Option<&'a FieldValue> {
    o.fields.iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

#[test]
fn two_declared_surface_recordings_reach_the_existing_surface_domain() {
    let usv = parse_profile(USV, "public").expect("authored USV profile");
    let survey = parse_profile(SURVEY, "public").expect("authored survey-boat profile");
    assert_eq!(
        usv.declared_platform_domain,
        Some(PlatformDomain::Surface),
        "the profile source declared surface explicitly"
    );
    assert_eq!(
        survey.declared_platform_domain,
        Some(PlatformDomain::Surface)
    );
    assert_eq!(usv.family, Family::Unknown);
    assert_eq!(survey.family, Family::Unknown);
    assert_ne!(usv.profile_id, survey.profile_id);
    assert_ne!(usv.fields.time, survey.fields.time);

    let usv_csv = b"record_time_us,sog_m_s,cog_rad,heading_rad,rudder_norm,sea_state_note\n\
1000,1.5,0.25,0.5,-0.125,calm\n\
1000,1.5,0.25,0.5,-0.125,calm\n\
2500,2.25,0.75,,0.0,\n";
    let report = TelemetryCsvReader
        .read_report_with_options(&usv, usv_csv, options())
        .expect("authored USV rows");
    assert_eq!(report.source_rows, 3);
    assert_eq!(report.observations.len(), 3);
    assert_eq!(
        report
            .source_columns
            .iter()
            .map(|c| c.name.as_str())
            .collect::<Vec<_>>(),
        [
            "record_time_us",
            "sog_m_s",
            "cog_rad",
            "heading_rad",
            "rudder_norm",
            "sea_state_note",
        ]
    );
    for o in &report.observations {
        assert_eq!(o.clock_basis, ClockBasis::Unknown);
        assert_eq!(o.t_boot_us, None);
        assert_eq!(o.anchor_unix_us, None);
    }
    assert_eq!(report.observations[0].t_ms, 1);
    assert_eq!(report.observations[2].t_ms, 2);
    assert_eq!(
        field(&report.observations[0], "sog_m_s"),
        Some(&FieldValue::F64(1.5))
    );
    assert_eq!(
        field(&report.observations[0], "sea_state_note"),
        Some(&FieldValue::Text("calm".into()))
    );
    assert_eq!(
        field(&report.observations[2], "heading_rad"),
        Some(&FieldValue::Blank),
        "an empty cell stays empty rather than becoming a heading of zero"
    );
    assert_eq!(report.observations[0].fields, report.observations[1].fields);

    let survey_csv = b"t_us_since_power_on,depth_below_transducer_m,bus_voltage_v,port_thruster_pwm_us,starboard_thruster_pwm_us\n\
4000,12.5,25.75,1500,1500\n\
9000,13.25,25.5,1600,1400\n";
    let survey_report = TelemetryCsvReader
        .read_report_with_options(&survey, survey_csv, options())
        .expect("authored survey-boat rows");
    assert_eq!(survey_report.source_rows, 2);
    assert_eq!(survey_report.observations.len(), 2);
    for o in &survey_report.observations {
        assert_eq!(o.clock_basis, ClockBasis::BootRelative);
        assert!(o.t_boot_us.is_some());
        assert_eq!(o.anchor_unix_us, None);
    }
    assert_eq!(survey_report.observations[0].t_boot_us, Some(4000));
    assert_eq!(
        field(&survey_report.observations[1], "depth_below_transducer_m"),
        Some(&FieldValue::F64(13.25))
    );
    assert_ne!(
        report.source_columns.len(),
        survey_report.source_columns.len()
    );
}

#[test]
fn underwater_stays_unknown_and_is_never_approximated_as_surface() {
    let rov = parse_profile(ROV, "public").expect("authored ROV profile");
    assert_eq!(
        rov.declared_platform_domain, None,
        "declaring nothing leaves the domain unestablished"
    );
    assert_eq!(rov.family, Family::Unknown);

    let csv = b"record_time_us,depth_m,tether_tension_n\n1000,18.5,120.0\n";
    let report = TelemetryCsvReader
        .read_report_with_options(&rov, csv, options())
        .expect("authored ROV rows");
    assert_eq!(report.observations.len(), 1);
    assert_eq!(
        field(&report.observations[0], "depth_m"),
        Some(&FieldValue::F64(18.5))
    );

    for label in ["underwater", "subsurface", "submarine", "rov", "auv", "sea"] {
        let attempted = ROV.replace(
            "source_role =",
            &format!("platform_domain = \"{label}\"\nsource_role ="),
        );
        assert!(
            parse_profile(&attempted, "public").is_err(),
            "'{label}' is not a common domain and must be refused, not mapped to Surface"
        );
    }
}

#[test]
fn established_families_and_their_domains_are_unchanged() {
    let ugv = parse_profile(
        include_str!("fixtures/turtlebot3-trajectory--profile.toml"),
        "public",
    )
    .expect("existing UGV profile");
    let unknown = parse_profile(include_str!("fixtures/nmea-wind--profile.toml"), "public")
        .expect("existing unknown-family profile");
    let fixed_wing = parse_profile(
        include_str!("fixtures/ardupilot-mode--profile.toml"),
        "public",
    )
    .expect("existing fixed-wing profile");
    let fpv = parse_profile(
        include_str!("fixtures/betaflight-raw-main--profile.toml"),
        "public",
    )
    .expect("existing FPV profile");
    for existing in [&ugv, &unknown, &fixed_wing, &fpv] {
        assert_eq!(
            existing.declared_platform_domain, None,
            "no existing profile gains a declaration it did not write"
        );
    }
    assert_eq!(ugv.family, Family::Ugv);
    assert_eq!(unknown.family, Family::Unknown);
    assert_eq!(fixed_wing.family, Family::FixedWing);
    assert_eq!(fpv.family, Family::Fpv);

    for (source, label) in [
        (
            include_str!("fixtures/turtlebot3-trajectory--profile.toml"),
            "surface",
        ),
        (
            include_str!("fixtures/ardupilot-mode--profile.toml"),
            "ground",
        ),
    ] {
        let attempted = source.replace(
            "source_role =",
            &format!("platform_domain = \"{label}\"\nsource_role ="),
        );
        assert!(
            parse_profile(&attempted, "public").is_err(),
            "an established family must not be re-declared as '{label}'"
        );
    }
}

#[test]
fn a_format_that_cannot_carry_the_declaration_refuses_it() {
    for format in [
        "px4_ulog",
        "mavlink_tlog",
        "ardupilot_dataflash_bin",
        "blackbox_decoded_csv",
        "pymavlink_dataflash_jsonl",
        "video_presence",
    ] {
        let attempted = USV.replace(
            "format = \"telemetry_csv_us\"",
            &format!("format = \"{format}\""),
        );
        assert!(
            parse_profile(&attempted, "public").is_err(),
            "'{format}' has no path for a declared domain and must refuse it"
        );
        let without = attempted.replace("platform_domain = \"surface\"\n", "");
        assert!(
            parse_profile(&without, "public").is_ok(),
            "only the declaration is refused, not the format"
        );
    }
    assert!(parse_profile(USV, "public").is_ok());
}

#[test]
fn family_remains_required_and_no_domain_comes_from_a_name() {
    let missing_family = USV.replace("family = \"unknown\"\n", "");
    assert!(
        parse_profile(&missing_family, "public").is_err(),
        "a declared domain must not stand in for a missing family"
    );
    let invalid_family = USV.replace("family = \"unknown\"", "family = \"usv\"");
    assert!(parse_profile(&invalid_family, "public").is_err());
    let invalid_domain = USV.replace(
        "platform_domain = \"surface\"",
        "platform_domain = \"Surface\"",
    );
    assert!(
        parse_profile(&invalid_domain, "public").is_err(),
        "the label is exact; no case folding, no near matches"
    );

    let boatish = ROV
        .replace(
            "profile_id = \"rov_underwater_undeclared_v1\"",
            "profile_id = \"usv_surface_boat_marine_vessel_v1\"",
        )
        .replace(
            "time = \"record_time_us\"",
            "time = \"boat_surface_time_us\"",
        );
    let named = parse_profile(&boatish, "public").expect("valid apart from its naming");
    assert_eq!(
        named.declared_platform_domain, None,
        "no domain is inferred from a profile id, a file name or a column name"
    );
    let csv = b"boat_surface_time_us,usv_surface_vessel_depth_m\n1000,4.5\n";
    let report = TelemetryCsvReader
        .read_report_with_options(&named, csv, options())
        .expect("rows read normally");
    assert_eq!(report.observations.len(), 1);
}
