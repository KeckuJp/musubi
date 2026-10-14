//! A profile-declared domain has to survive the **library** path, not just a development
//! example: reader to `analyze` to `build_steps` to the sealed `PlatformState`, and out
//! through the existing CoT publisher. Inputs are authored here; no real vessel or log.
use musubi_reference_fanout::{StepKind, build_steps, domain_of};
use musubi_reference_pipeline::{InputFile, Knowledge, ObservationWindow, PipelineOut, analyze};
use musubi_reference_types::{
    ChannelId, DOMAIN_BASIS_DECLARED, DOMAIN_BASIS_FAMILY, ExpectationModel, Family, FamilyProfile,
    SignatureCatalog, SourceRole,
};
use musubi_types::{ComObject, PlatformDomain};

/// The boot-relative surface layout. The fan-out places every step on the wall clock, so
/// the recording needs a clock the pipeline can align; an unknown-clock recording carries
/// no `wall_ms` and produces no steps at all, which is existing behaviour this change
/// neither uses nor alters.
const SURVEY: &str =
    include_str!("../../../profiles/declared/surface-usv/survey-boat-profile.toml");

const T0_MS: i64 = 1_000;
const END_MS: i64 = 61_000;

/// Authored rows. `GPS_coord[0]`/`GPS_coord[1]` are the decimal-degree column names the
/// existing position table already knows; this change adds no position field and reads
/// nothing from the other column names.
fn csv() -> Vec<u8> {
    b"t_us_since_power_on,GPS_coord[0],GPS_coord[1],depth_below_transducer_m\n\
1000000,35.5,139.75,12.5\n\
61000000,35.5078125,139.75,13.25\n"
        .to_vec()
}

fn surface_profile() -> FamilyProfile {
    musubi_reference_readers::profile::parse_profile(SURVEY, "public").expect("authored profile")
}

fn run(profiles: Vec<FamilyProfile>, files: Vec<InputFile>) -> PipelineOut {
    let knowledge = Knowledge {
        profiles,
        catalog: SignatureCatalog {
            version: "test".into(),
            signatures: vec![],
        },
    };
    analyze(
        files,
        &knowledge,
        ObservationWindow::Explicit {
            t0_ms: T0_MS,
            end_ms: END_MS,
        },
    )
    .expect("analyze")
}

fn file(profile: &FamilyProfile, asset_id: &str, file_name: &str) -> InputFile {
    InputFile {
        asset_id: asset_id.into(),
        family: profile.family,
        source: profile.source_role,
        format_id: profile.format.clone(),
        file_name: file_name.into(),
        bytes: csv(),
    }
}

/// Every sealed step of the run: kind, the common domain and the MARK provenance.
fn sealed(out: &PipelineOut) -> Vec<(StepKind, PlatformDomain, Vec<String>)> {
    let steps: Vec<_> = build_steps(out)
        .iter()
        .map(|s| {
            let ComObject::PlatformState(ps) = &s.envelope.observation else {
                panic!("reference example steps are platform states");
            };
            (s.kind, ps.platform_domain, s.mark().provenance.clone())
        })
        .collect();
    assert!(
        !steps.is_empty(),
        "the authored rows must reach the common output, or this proves nothing"
    );
    steps
}

/// The normal end-to-end library path: a declared surface recording reaches the sealed
/// common `PlatformState` and the CoT a consumer actually receives.
#[test]
fn declared_surface_reaches_the_sealed_platform_state_and_cot() {
    let usv = surface_profile();
    let out = run(vec![usv.clone()], vec![file(&usv, "usv-01", "usv.csv")]);
    for (kind, domain, provenance) in &sealed(&out) {
        assert_eq!(
            *domain,
            PlatformDomain::Surface,
            "{kind:?} step lost the declared domain"
        );
        // The consumer is told the domain was configured by a profile, not observed.
        assert!(
            provenance
                .iter()
                .any(|p| p == "domain-declared-by-profile/Surface"),
            "provenance must say where the domain came from: {provenance:?}"
        );
        assert!(
            provenance.iter().any(|p| p.starts_with("profile/")),
            "existing profile provenance is kept: {provenance:?}"
        );
    }

    // Through the existing northbound publisher the domain is visible as the CoT battle
    // dimension: `a-u-S` is surface, `a-u-G` ground, `a-u-A` air.
    let fanned = musubi_reference_fanout::run(&build_steps(&out), out.t0_ms);
    let cot = musubi_reference_fanout::concat_by_dialect(&fanned)
        .remove("cot")
        .expect("CoT is one of the existing dialects");
    let cot = String::from_utf8(cot).expect("CoT is XML text");
    assert!(cot.contains("type=\"a-u-S\""), "CoT type should be surface");
    assert!(!cot.contains("type=\"a-u-G\""));
    assert!(!cot.contains("type=\"a-u-A\""));
    // The basis travels with it: CoT carries provenance in its remarks.
    assert!(cot.contains("domain-declared-by-profile/Surface"));
}

/// The same path with a profile that declares nothing behaves exactly as before, and
/// nothing claims a domain it was not given.
#[test]
fn an_undeclared_recording_is_unchanged_and_adds_no_provenance() {
    let mut undeclared = surface_profile();
    undeclared.profile_id = "undeclared_recorded_v1".into();
    undeclared.declared_platform_domain = None;
    let out = run(
        vec![undeclared.clone()],
        vec![file(&undeclared, "asset-01", "recording.csv")],
    );
    for (_, domain, provenance) in &sealed(&out) {
        assert_eq!(*domain, PlatformDomain::Unknown);
        assert_eq!(domain_of(Family::Unknown), *domain);
        assert!(
            !provenance.iter().any(|p| p.starts_with("domain-declared")),
            "a profile that declared nothing must add nothing: {provenance:?}"
        );
    }
}

/// Configuration never outranks a family that already establishes a domain, and two
/// sources that contradict each other establish nothing rather than one quietly winning.
#[test]
fn a_caller_declaration_never_overrides_or_silently_wins() {
    // Hand-built, because the parser refuses this pairing outright. Even so the shared
    // resolution point keeps the family's Ground.
    let mut forced = surface_profile();
    forced.family = Family::Ugv;
    forced.declared_platform_domain = Some(PlatformDomain::Surface);
    assert_eq!(
        forced.configured_platform_domain(),
        (PlatformDomain::Ground, DOMAIN_BASIS_FAMILY),
        "a family-established domain is not overridden by a caller hint"
    );

    // Two sources of one asset declaring different domains: nothing is established, and
    // neither declaration is picked. The loader selects a profile by family and format
    // alone, so it cannot hand one asset two different declarations today; the second is
    // set on the analysed run to reach the resolution point that must survive it anyway.
    let usv = surface_profile();
    let mut out = run(
        vec![usv.clone()],
        vec![
            file(&usv, "usv-01", "usv.csv"),
            file(&usv, "usv-01", "usv-gcs.csv"),
        ],
    );
    assert_eq!(out.assets.len(), 1);
    assert_eq!(out.assets[0].sources.len(), 2, "both sources were read");
    assert_eq!(
        out.assets[0].sources[1].profile.source_role,
        SourceRole::RecordedExport
    );
    out.assets[0].sources[1].profile.declared_platform_domain = Some(PlatformDomain::Air);
    out.assets[0].sources[1].profile.profile_id = "second_declared_air_v1".into();
    for (_, domain, provenance) in sealed(&out) {
        assert_eq!(
            domain,
            PlatformDomain::Unknown,
            "contradicting declarations must not pick one"
        );
        // Unknown here does not mean nobody declared anything, and the step says so.
        assert!(
            provenance.contains(&"domain-declarations-conflict-unresolved/Air+Surface".to_string()),
            "the conflict must be explained: {provenance:?}"
        );
        assert!(
            provenance.contains(&"domain-falls-back-to-family/Unknown".to_string()),
            "and so must the answer it fell back to: {provenance:?}"
        );
        for declaring in [
            "domain-declaring-profile/survey_boat_surface_declared_v1@1",
            "domain-declaring-profile/second_declared_air_v1@1",
        ] {
            assert!(
                provenance.contains(&declaring.to_string()),
                "both declarations are named: {provenance:?}"
            );
        }
        assert!(
            !provenance
                .iter()
                .any(|p| p.starts_with("domain-declared-by-profile/")),
            "nothing was established, so nothing may claim to be: {provenance:?}"
        );
    }
}

/// A second source of the same asset, in another format and declaring nothing, inherits
/// the asset's domain — so it must inherit the explanation too, or a consumer sees a bare
/// Surface on a recording whose own profile never said anything.
#[test]
fn every_inheriting_step_carries_the_asset_basis_and_the_declaring_profile() {
    // The declaring source, with an expectation so the run also produces absence steps.
    let mut declaring = surface_profile();
    declaring.expectations.push(ExpectationModel {
        expectation_id: "onboard-cadence".into(),
        channel: ChannelId::Onboard,
        cadence_ms: 1_000,
        grace_k: 1,
        frozen_ms: None,
        frozen_fields: None,
        required_fields: None,
        declared_window_only: false,
    });
    // A different format entirely, declaring no domain. Its rows are later, so the last
    // observation steps take their position — and their provenance — from this source.
    let companion = musubi_reference_readers::profile::parse_profile(
        r#"
profile_id = "companion_dataflash_v1"
version = "1"
family = "unknown"
source_role = "gcs"
format = "pymavlink_dataflash_jsonl"
extensions = ["jsonl"]
default_clock_basis = "gps_locked"
channels = ["onboard"]
[fields]
time = "TimeUS"
"#,
        "public",
    )
    .expect("authored companion profile");
    assert_eq!(companion.declared_platform_domain, None);

    let jsonl = b"{\"meta\":{\"type\":\"GPS\",\"timestamp\":1.0},\"data\":{\"TimeUS\":40000000,\"Lat\":355200000,\"Lng\":1397500000,\"Alt\":150}}\n\
{\"meta\":{\"type\":\"GPS\",\"timestamp\":2.0},\"data\":{\"TimeUS\":90000000,\"Lat\":355300000,\"Lng\":1397500000,\"Alt\":150}}\n"
        .to_vec();
    let mut companion_file = file(&companion, "usv-01", "companion.jsonl");
    companion_file.bytes = jsonl;

    let out = run(
        vec![declaring.clone(), companion.clone()],
        vec![file(&declaring, "usv-01", "usv.csv"), companion_file],
    );
    assert_eq!(out.assets.len(), 1);
    assert_eq!(out.assets[0].sources.len(), 2, "both sources were read");

    let steps = sealed(&out);
    for (kind, domain, provenance) in &steps {
        assert_eq!(*domain, PlatformDomain::Surface, "{kind:?} lost the domain");
        assert!(
            provenance.contains(&"domain-declared-by-profile/Surface".to_string())
                && provenance.contains(
                    &"domain-declaring-profile/survey_boat_surface_declared_v1@1".to_string()
                ),
            "{kind:?} inherited the domain without the basis: {provenance:?}"
        );
    }
    // The point of the test: a step whose own record came from the profile that declared
    // nothing still says where the Surface came from.
    let inherited: Vec<_> = steps
        .iter()
        .filter(|(_, _, p)| p.contains(&"profile/companion_dataflash_v1@1".to_string()))
        .collect();
    assert!(
        !inherited.is_empty(),
        "the non-declaring source must supply at least one step: {steps:?}"
    );
    assert!(
        steps.iter().any(|(k, _, _)| *k == StepKind::Absence),
        "an absence step should inherit it too: {steps:?}"
    );
    // Cause steps have no single source at all, so they are the case most likely to lose
    // an explanation; the loop above covers them too.
    assert!(
        steps.iter().any(|(k, _, _)| *k == StepKind::Cause),
        "a cause candidate should inherit it as well: {steps:?}"
    );
}

/// The resolution point is shared, so no consumer can grow a second family table.
#[test]
fn one_resolution_point_backs_both_the_family_table_and_the_declaration() {
    for family in [Family::Unknown, Family::Ugv, Family::FixedWing, Family::Fpv] {
        assert_eq!(domain_of(family), family.platform_domain());
    }
    assert_eq!(
        surface_profile().configured_platform_domain(),
        (PlatformDomain::Surface, DOMAIN_BASIS_DECLARED)
    );
}
