use std::{collections::BTreeMap, path::Path};

use musubi_integration_case::{
    CandidateKindV1, IntegrationCaseManifestV1, VerifiedReportArtifactV1, adapter_hold_report,
    load_profile_candidate, strict_manifest_from_json, verified_adapter_hold_from_json,
    verified_profile_report_from_json, verify_profile_case,
};

fn main() {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if let Err(error) = run(&args) {
        eprintln!("musubi-integration-case: {error}");
        std::process::exit(2);
    }
}

fn run(args: &[String]) -> Result<(), String> {
    if args.len() < 2 || args.iter().any(|arg| arg == "--help" || arg == "-h") {
        print_help();
        return Ok(());
    }
    let command = (&args[0], &args[1]);
    let options = parse_options(&args[2..])?;
    match command {
        (scope, action) if scope == "manifest" && action == "validate" => {
            let manifest = load_manifest(required(&options, "--case")?)?;
            let output = serde_json::json!({
                "schema_version": "1",
                "case_id": manifest.case_id,
                "manifest_digest": manifest.manifest_digest().map_err(|error| error.to_string())?,
                "valid": true,
            });
            println!(
                "{}",
                serde_json::to_string_pretty(&output).map_err(|error| error.to_string())?
            );
        }
        (scope, action) if scope == "profile" && action == "verify" => {
            let manifest = load_manifest(required(&options, "--case")?)?;
            let candidate = load_profile_candidate(
                Path::new(required(&options, "--candidate-root")?),
                &manifest,
            )?;
            let sample = read(required(&options, "--against")?)?;
            let oracle = read(required(&options, "--gold")?)?;
            let report = verify_profile_case(
                &manifest,
                required(&options, "--requirements-commit")?,
                required(&options, "--implementation-commit")?,
                &candidate,
                &sample,
                &oracle,
            )?;
            println!(
                "{}",
                serde_json::to_string_pretty(&report).map_err(|error| error.to_string())?
            );
        }
        (scope, action) if scope == "adapter" && action == "hold" => {
            let manifest = load_manifest(required(&options, "--case")?)?;
            let candidate = read(required(&options, "--candidate")?)?;
            let report = adapter_hold_report(
                &manifest,
                required(&options, "--requirements-commit")?,
                required(&options, "--implementation-commit")?,
                &candidate,
            )
            .map_err(|error| error.to_string())?;
            println!(
                "{}",
                serde_json::to_string_pretty(&report).map_err(|error| error.to_string())?
            );
        }
        (scope, action) if scope == "report" && action == "verify" => {
            let manifest = load_manifest(required(&options, "--case")?)?;
            let bytes = read(required(&options, "--report")?)?;
            let verified = verify_stored_report(&options, &manifest, &bytes)?;
            let report = verified.report();
            let output = serde_json::json!({
                "schema_version": "1",
                "case_id": &report.case_id,
                "manifest_digest": &report.input_manifest_digest,
                "report_digest": report.receipt_digest().map_err(|error| error.to_string())?,
                "report_kind": verified.artifact_kind(),
                "valid": true,
            });
            println!(
                "{}",
                serde_json::to_string_pretty(&output).map_err(|error| error.to_string())?
            );
        }
        _ => return Err("unknown command; use --help".into()),
    }
    Ok(())
}

fn verify_stored_report(
    options: &BTreeMap<String, String>,
    manifest: &IntegrationCaseManifestV1,
    bytes: &[u8],
) -> Result<VerifiedReportArtifactV1, String> {
    match manifest.candidate_kind {
        CandidateKindV1::Profile => {
            let candidate = load_profile_candidate(
                Path::new(required(options, "--candidate-root")?),
                manifest,
            )?;
            let sample = read(required(options, "--against")?)?;
            let oracle = read(required(options, "--gold")?)?;
            verified_profile_report_from_json(
                bytes,
                manifest,
                required(options, "--requirements-commit")?,
                required(options, "--implementation-commit")?,
                &candidate,
                &sample,
                &oracle,
            )
        }
        CandidateKindV1::Adapter => {
            let candidate = read(required(options, "--candidate")?)?;
            verified_adapter_hold_from_json(
                bytes,
                manifest,
                required(options, "--requirements-commit")?,
                required(options, "--implementation-commit")?,
                &candidate,
            )
        }
    }
}

fn parse_options(args: &[String]) -> Result<BTreeMap<String, String>, String> {
    if !args.len().is_multiple_of(2) {
        return Err("every option requires one value".into());
    }
    let mut options = BTreeMap::new();
    for pair in args.chunks_exact(2) {
        if !pair[0].starts_with("--") || options.insert(pair[0].clone(), pair[1].clone()).is_some()
        {
            return Err(format!("invalid or duplicate option: {}", pair[0]));
        }
    }
    Ok(options)
}

fn required<'a>(options: &'a BTreeMap<String, String>, key: &str) -> Result<&'a str, String> {
    options
        .get(key)
        .map(String::as_str)
        .ok_or_else(|| format!("missing {key}"))
}

fn load_manifest(path: &str) -> Result<IntegrationCaseManifestV1, String> {
    let bytes = read(path)?;
    strict_manifest_from_json(&bytes)
}

fn read(path: &str) -> Result<Vec<u8>, String> {
    std::fs::read(Path::new(path)).map_err(|error| format!("{path}: {error}"))
}

fn print_help() {
    println!(
        "musubi-integration-case (offline candidate verification)\n\
         \n\
         manifest validate --case <manifest.json>\n\
         profile verify --case <manifest.json> --candidate-root <isolated-directory> \\\n\
           --against <sample.csv> --gold <oracle.json> \\\n\
           --requirements-commit <sha> --implementation-commit <sha>\n\
         adapter hold --case <manifest.json> --candidate <candidate-artifact> \\\n\
           --requirements-commit <sha> --implementation-commit <sha>\n\
         report verify --case <manifest.json> --report <verified-artifact.json> \\\n\
           [Profile: --candidate-root <isolated-directory> --against <sample.csv> --gold <oracle.json>] \\\n\
           [Adapter: --candidate <candidate-artifact>] \\\n\
           --requirements-commit <sha> --implementation-commit <sha>\n\
         \n\
         Profile report verification replays the complete score-bound bundle from all frozen inputs.\n\
         Adapter report verification regenerates and accepts only the exact frozen-runner HOLD artifact; candidate bytes are hashed but never executed.\n\
         The CLI never invokes an agent, executes manifest commands, loads a candidate into a runtime, or creates a human seal."
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn options_reject_duplicates() {
        let args = vec![
            "--case".to_string(),
            "a".to_string(),
            "--case".to_string(),
            "b".to_string(),
        ];
        assert!(parse_options(&args).is_err());
    }
}
