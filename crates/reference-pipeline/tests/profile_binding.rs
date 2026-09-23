use musubi_reference_pipeline::{InputFile, Knowledge, ObservationWindow, PipelineOut, analyze};
use musubi_reference_readers::profile::parse_profile;
use musubi_reference_types::{FamilyProfile, SignatureCatalog, SourceRole};

const USV: &str = include_str!("fixtures/unknown-adapter--surface-usv--profile.toml");
const ROV: &str = include_str!("fixtures/unknown-adapter--surface-usv--rov-profile.toml");

const CSV: &[u8] = b"record_time_us,depth_m,sog_m_s\n1000,4.5,1.5\n2000,4.75,1.25\n";

fn profile(source: &str) -> FamilyProfile {
    parse_profile(source, "public").expect("authored profile")
}

fn renamed(source: &str, id: &str) -> FamilyProfile {
    let mut p = profile(source);
    p.profile_id = id.into();
    p
}

fn input(role: SourceRole) -> InputFile {
    InputFile {
        asset_id: "asset-01".into(),
        family: musubi_reference_types::Family::Unknown,
        source: role,
        format_id: "telemetry_csv_us".into(),
        file_name: "recording.csv".into(),
        bytes: CSV.to_vec(),
    }
}

fn analyzed(profiles: Vec<FamilyProfile>, file: InputFile) -> Result<PipelineOut, String> {
    let knowledge = Knowledge {
        profiles,
        catalog: SignatureCatalog {
            version: "test".into(),
            signatures: vec![],
        },
    };
    analyze(
        vec![file],
        &knowledge,
        ObservationWindow::Explicit {
            t0_ms: 1,
            end_ms: 2,
        },
    )
}

fn bound_profile(out: &PipelineOut) -> &FamilyProfile {
    &out.assets[0].sources[0].profile
}

#[test]
fn a_declaration_never_wins_by_profile_order() {
    let surface = profile(USV);
    let undeclared = profile(ROV);
    let air = {
        let mut p = parse_profile(
            &USV.replace("platform_domain = \"surface\"", "platform_domain = \"air\""),
            "public",
        )
        .expect("an air declaration parses the same way");
        p.profile_id = "second_declared_air_v1".into();
        p
    };
    assert_eq!(surface.source_role, undeclared.source_role);
    assert_eq!(surface.source_role, air.source_role);

    for (a, b, case) in [
        (
            surface.clone(),
            undeclared.clone(),
            "declared vs undeclared",
        ),
        (undeclared.clone(), surface.clone(), "undeclared first"),
        (surface.clone(), air.clone(), "two different declarations"),
        (air.clone(), surface.clone(), "the other way round"),
    ] {
        let err = analyzed(vec![a, b], input(SourceRole::RecordedExport))
            .expect_err(&format!("{case} must not be resolved by order"));
        assert!(
            err.contains("ambiguous profile binding"),
            "{case}: refusal must say why, got {err}"
        );
        assert!(
            err.contains("recording.csv") && err.contains("declares a"),
            "{case}: refusal must name the file and the reason, got {err}"
        );
    }
}

#[test]
fn a_single_matching_declaring_profile_still_binds() {
    let out = analyzed(vec![profile(USV)], input(SourceRole::RecordedExport))
        .expect("one candidate is never ambiguous");
    assert_eq!(bound_profile(&out).profile_id, "usv_surface_declared_v1");
    assert!(bound_profile(&out).declared_platform_domain.is_some());
    assert_eq!(out.assets[0].sources[0].obs.len(), 2);
}

#[test]
fn candidates_without_a_declaration_keep_first_match() {
    let first = renamed(ROV, "first_undeclared_v1");
    let second = renamed(ROV, "second_undeclared_v1");
    for (a, b) in [
        (first.clone(), second.clone()),
        (second.clone(), first.clone()),
    ] {
        let expected = a.profile_id.clone();
        let out = analyzed(vec![a, b], input(SourceRole::RecordedExport))
            .expect("no declaration is at stake, so nothing is refused");
        assert_eq!(bound_profile(&out).profile_id, expected);
        assert_eq!(bound_profile(&out).declared_platform_domain, None);
    }
}

#[test]
fn the_declared_source_role_binds_exactly_when_it_can() {
    let surface = profile(USV);
    let mut gcs_undeclared = renamed(ROV, "gcs_undeclared_v1");
    gcs_undeclared.source_role = SourceRole::Gcs;

    let out = analyzed(
        vec![surface.clone(), gcs_undeclared.clone()],
        input(SourceRole::RecordedExport),
    )
    .expect("the file's own role singles one out");
    assert_eq!(bound_profile(&out).profile_id, "usv_surface_declared_v1");

    let out = analyzed(
        vec![surface.clone(), gcs_undeclared.clone()],
        input(SourceRole::Gcs),
    )
    .expect("the file's own role singles the other one out");
    assert_eq!(bound_profile(&out).profile_id, "gcs_undeclared_v1");
    assert_eq!(
        bound_profile(&out).declared_platform_domain,
        None,
        "a declaration on the profile that was not selected establishes nothing"
    );
}
