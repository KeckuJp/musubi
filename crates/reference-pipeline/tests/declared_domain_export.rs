use musubi_reference_pipeline::export::{
    absences_csv, claims_csv, export_all_with_declared_domain,
};
use musubi_reference_pipeline::{InputFile, Knowledge, ObservationWindow, PipelineOut, analyze};
use musubi_reference_types::{ChannelId, ExpectationModel, SignatureCatalog};

fn fixture(declaration: &str) -> (PipelineOut, Knowledge) {
    fixture_basis(declaration, "boot_relative")
}

fn fixture_basis(declaration: &str, basis: &str) -> (PipelineOut, Knowledge) {
    let text = include_str!("fixtures/unknown-adapter--surface-usv--survey-boat-profile.toml")
        .replace("platform_domain = \"surface\"", declaration)
        .replace("boot_relative", basis);
    let mut profile = musubi_reference_readers::profile::parse_profile(&text, "public").unwrap();
    profile.expectations.push(ExpectationModel {
        expectation_id: "saved-cadence".into(),
        channel: ChannelId::Onboard,
        cadence_ms: 1_000,
        grace_k: 1,
        frozen_ms: None,
        frozen_fields: None,
        required_fields: None,
        declared_window_only: false,
    });
    let input = InputFile {
        asset_id: "authored-vessel".into(),
        family: profile.family,
        source: profile.source_role,
        format_id: profile.format.clone(),
        file_name: "saved.csv".into(),
        bytes: b"t_us_since_power_on,depth_below_transducer_m\n1000000,12.5\n61000000,13.25\n"
            .to_vec(),
    };
    let k = Knowledge {
        profiles: vec![profile],
        catalog: SignatureCatalog {
            version: "test".into(),
            signatures: vec![],
        },
    };
    let out = analyze(
        vec![input],
        &k,
        ObservationWindow::Explicit {
            t0_ms: 1_000,
            end_ms: 61_000,
        },
    )
    .unwrap();
    assert!(!out.noticed.is_empty() && !out.claims.is_empty());
    (out, k)
}

fn exported(out: &PipelineOut, k: &Knowledge, tag: &str) -> Vec<String> {
    let dir = std::env::temp_dir().join(format!("musubi-domain-{}-{tag}", std::process::id()));
    let written = export_all_with_declared_domain(out, k, None, &dir).unwrap();
    assert_eq!(written.len(), 3);
    let result = ["absences.csv", "cause_claims.csv"]
        .iter()
        .map(|name| std::fs::read_to_string(dir.join(name)).unwrap())
        .collect();
    std::fs::remove_dir_all(dir).unwrap();
    result
}

#[test]
fn declared_domain_reaches_both_exports_without_replacing_default_columns() {
    let (out, k) = fixture("platform_domain = \"surface\"");
    for (new, old) in exported(&out, &k, "surface")
        .iter()
        .zip([absences_csv(&out), claims_csv(&out, &k)])
    {
        assert!(
            new.lines()
                .next()
                .unwrap()
                .ends_with("platform_domain,platform_domain_basis,platform_domain_provenance")
        );
        assert_eq!(new.lines().count(), old.lines().count());
        for line in new.lines().skip(1) {
            assert!(line.contains(",Surface,profile_declared_platform_domain,"));
            assert!(line.contains("domain-declaring-profile/survey_boat_surface_declared_v1@1"));
        }
        assert!(!old.contains("platform_domain"));
        let previous = parse_csv(&old);
        let current = parse_csv(new);
        let version = previous[0]
            .iter()
            .position(|s| s == "export_schema_version")
            .unwrap();
        for (new_row, old_row) in current.iter().skip(1).zip(previous.iter().skip(1)) {
            assert_eq!(new_row.len(), old_row.len() + 3);
            assert_eq!(new_row[version], "4");
            for index in 0..old_row.len() {
                if index != version {
                    assert_eq!(new_row[index], old_row[index]);
                }
            }
            let tokens: Vec<String> = serde_json::from_str(new_row.last().unwrap()).unwrap();
            assert_eq!(
                tokens,
                vec![
                    "domain-declared-by-profile/Surface",
                    "domain-declaring-profile/survey_boat_surface_declared_v1@1"
                ]
            );
        }
    }
}

#[test]
fn conflict_and_no_declaration_remain_distinct_unknowns() {
    let (mut out, k) = fixture("platform_domain = \"surface\"");
    let mut second = out.assets[0].sources[0].clone();
    second.profile = musubi_reference_readers::profile::parse_profile(
        &include_str!("fixtures/unknown-adapter--surface-usv--survey-boat-profile.toml")
            .replace("surface\"", "air\"")
            .replace("survey_boat_surface_declared_v1", "other_air"),
        "public",
    )
    .unwrap();
    out.assets[0].sources.push(second);
    for csv in exported(&out, &k, "conflict") {
        for row in csv.lines().skip(1) {
            assert!(row.contains(",Unknown,profile_declarations_conflict_unresolved,"));
            assert!(row.contains("domain-declarations-conflict-unresolved/Air+Surface"));
            assert!(row.contains("domain-declaring-profile/other_air@1"));
            assert!(row.contains("domain-declaring-profile/survey_boat_surface_declared_v1@1"));
        }
    }
    let (out, k) = fixture("");
    for csv in exported(&out, &k, "unknown") {
        for row in csv.lines().skip(1) {
            assert!(row.ends_with(",Unknown,profile_declared_family,[]"));
        }
    }
}

#[test]
fn missing_or_ambiguous_asset_binding_refuses_before_writing() {
    let (mut out, k) = fixture("platform_domain = \"surface\"");
    let dir = std::env::temp_dir().join(format!("musubi-domain-refused-{}", std::process::id()));
    out.assets.push(out.assets[0].clone());
    assert!(export_all_with_declared_domain(&out, &k, None, &dir).is_err());
    assert!(!dir.exists());
    out.assets.clear();
    assert!(export_all_with_declared_domain(&out, &k, None, &dir).is_err());
    assert!(!dir.exists());
    let (mut mismatch, k) = fixture("platform_domain = \"surface\"");
    mismatch.noticed[0].negative.subject.asset_id = "other".into();
    assert!(export_all_with_declared_domain(&mismatch, &k, None, &dir).is_err());
    assert!(!dir.exists());
    let (mut wrong_family, k) = fixture("platform_domain = \"surface\"");
    wrong_family.assets[0].family = musubi_reference_types::Family::Ugv;
    assert!(export_all_with_declared_domain(&wrong_family, &k, None, &dir).is_err());
    assert!(!dir.exists());
}

#[test]
fn unknown_clock_keeps_native_absence_and_json_preserves_declaring_id() {
    let (mut out, k) = fixture_basis("platform_domain = \"surface\"", "unknown");
    out.assets[0].sources[0].profile.profile_id = "caller,\"quoted\"\nline".into();
    let csv = exported(&out, &k, "unknown-clock");
    let rows = parse_csv(&csv[0]);
    let at =
        |row: &[String], name: &str| row[rows[0].iter().position(|h| h == name).unwrap()].clone();
    assert_eq!(rows.len() - 1, out.noticed.len() + out.unmapped.len());
    for row in rows.iter().skip(1) {
        assert_eq!(at(row, "clock_basis"), "unknown");
        assert_eq!(at(row, "absent_since_wall_ms"), "");
        assert_eq!(at(row, "wall_mapping"), "unmapped_unknown_clock");
        assert!(!at(row, "absent_since_native_ms").is_empty());
        assert_eq!(at(row, "platform_domain"), "Surface");
        let tokens: Vec<String> =
            serde_json::from_str(&at(row, "platform_domain_provenance")).unwrap();
        assert!(tokens.contains(&"domain-declaring-profile/caller,\"quoted\"\nline@1".into()));
    }
}

fn parse_csv(s: &str) -> Vec<Vec<String>> {
    let (mut rows, mut row, mut cell, mut quoted) = (Vec::new(), Vec::new(), String::new(), false);
    let mut it = s.chars().peekable();
    while let Some(ch) = it.next() {
        if quoted {
            if ch == '"' {
                if it.peek() == Some(&'"') {
                    it.next();
                    cell.push('"');
                } else {
                    quoted = false;
                }
            } else {
                cell.push(ch);
            }
        } else {
            match ch {
                '"' => quoted = true,
                ',' => row.push(std::mem::take(&mut cell)),
                '\n' => {
                    row.push(std::mem::take(&mut cell));
                    rows.push(std::mem::take(&mut row));
                }
                _ => cell.push(ch),
            }
        }
    }
    assert!(
        !quoted && cell.is_empty() && row.is_empty(),
        "CSV must end with a complete row and a trailing LF"
    );
    rows
}
