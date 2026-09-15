#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::too_many_lines,
    clippy::missing_const_for_fn,
    clippy::module_name_repetitions
)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

mod app;
mod detached;
mod detached_app;
mod png;
mod report;
mod report_app;
#[cfg(test)]
mod report_tests;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use clap::Parser;
use musubi_reference_pipeline::export::export_all;
use musubi_reference_pipeline::load::{Loaded, load_dir};
use musubi_reference_pipeline::{Knowledge, PipelineOut, analyze, metrics_from};
use musubi_reference_readers::eval::Metrics;
use musubi_reference_types::Family;

#[derive(Parser, Debug)]
#[command(name = "musubi-reference-viewer", version, about)]
struct Args {
    #[arg(long = "in")]
    input: PathBuf,
    #[arg(long, conflicts_with_all = ["profiles", "asset_family", "export"])]
    detached: bool,
    #[arg(long, conflicts_with_all = ["detached", "profiles", "asset_family", "export"])]
    report: bool,
    #[arg(long, requires = "report", conflicts_with = "screenshot")]
    check: bool,
    #[arg(long)]
    profiles: Option<PathBuf>,
    #[arg(long)]
    export: Option<PathBuf>,
    #[arg(long)]
    screenshot: Option<PathBuf>,
    #[arg(long = "asset-family")]
    asset_family: Vec<String>,
}

fn parse_family_hints(specs: &[String]) -> Result<BTreeMap<String, Family>, String> {
    let mut m = BTreeMap::new();
    for s in specs {
        let (a, f) = s
            .split_once('=')
            .ok_or_else(|| format!("bad --asset-family '{s}' (ASSET=FAMILY)"))?;
        let fam = Family::parse(f).ok_or_else(|| format!("unknown family '{f}'"))?;
        m.insert(a.to_string(), fam);
    }
    Ok(m)
}

fn find_profiles_root(explicit: Option<&Path>) -> Result<PathBuf, String> {
    if let Some(p) = explicit {
        return if p.join("public").is_dir() {
            Ok(p.to_path_buf())
        } else {
            Err(format!("--profiles {}: no public/ directory", p.display()))
        };
    }
    let mut tried = Vec::new();
    let mut cands = vec![PathBuf::from("profiles")];
    if let Ok(exe) = std::env::current_exe()
        && let Some(dir) = exe.parent()
    {
        cands.push(dir.join("profiles"));
        cands.push(dir.join("../../profiles"));
    }
    for c in cands {
        if c.join("public").is_dir() {
            return Ok(c);
        }
        tried.push(c.display().to_string());
    }
    Err(format!(
        "profiles root not found (tried: {}); pass --profiles <dir>",
        tried.join(", ")
    ))
}

pub struct Session {
    pub knowledge: Knowledge,
    pub loaded: Loaded,
    pub out: PipelineOut,
    pub metrics: Option<Metrics>,
    pub input_dir: PathBuf,
    pub profiles_root: PathBuf,
}

fn open_session(args: &Args) -> Result<Session, String> {
    let profiles_root = find_profiles_root(args.profiles.as_deref())?;
    let knowledge = Knowledge::load(&profiles_root)?;
    let hints = parse_family_hints(&args.asset_family)?;
    let loaded = load_dir(&args.input, &knowledge.profiles, &hints)?;
    let out = analyze(loaded.files.clone(), &knowledge, loaded.window)?;
    let metrics = if loaded.ground_truth.is_empty() {
        None
    } else {
        let duration = loaded.duration_ms.unwrap_or(out.end_ms - out.t0_ms);
        Some(metrics_from(
            &loaded.ground_truth,
            loaded.t0_ms.unwrap_or(out.t0_ms),
            out.assets.len(),
            duration,
            &out,
            &knowledge,
        ))
    };
    Ok(Session {
        knowledge,
        loaded,
        out,
        metrics,
        input_dir: args.input.clone(),
        profiles_root,
    })
}

fn main() {
    let args = Args::parse();
    if args.report {
        let report = match report::load(&args.input) {
            Ok(report) => report,
            Err(error) => {
                eprintln!("Report could not be opened: {error}");
                std::process::exit(2);
            }
        };
        if args.check {
            println!(
                "Report valid: {} observations; unsealed; embedded source bytes match. Profile and meaning hashes are declarations only.",
                report.document["observation_count"]
            );
            return;
        }
        if let Err(error) = report_app::run(report, args.screenshot.clone()) {
            eprintln!("Viewer could not open: {error}");
            std::process::exit(4);
        }
        return;
    }
    if args.detached {
        let input = if args.input.is_dir() {
            args.input.join("observations.ndjson")
        } else {
            args.input.clone()
        };
        let recording = match detached::load(&input) {
            Ok(recording) => recording,
            Err(error) => {
                eprintln!("musubi-reference-viewer: {error}");
                std::process::exit(2);
            }
        };
        eprintln!(
            "loaded {} detached observations / {}",
            recording.records.len(),
            recording.digest
        );
        if let Err(error) = detached_app::run(recording, args.screenshot.clone()) {
            eprintln!("viewer: {error}");
            std::process::exit(4);
        }
        return;
    }
    let session = match open_session(&args) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("musubi-reference-viewer: {e}");
            std::process::exit(2);
        }
    };
    for (name, why) in &session.loaded.skipped {
        eprintln!("skipped {name}: {why}");
    }
    eprintln!(
        "loaded {} files / {} assets / {} absences / {} cause windows (profiles: {})",
        session.out.files.len(),
        session.out.assets.len(),
        session.out.noticed.len(),
        session.out.claims.len(),
        session.profiles_root.display()
    );
    if let Some(dir) = &args.export {
        match export_all(
            &session.out,
            &session.knowledge,
            session.metrics.as_ref(),
            dir,
        ) {
            Ok(written) => {
                for p in written {
                    eprintln!("wrote {}", p.display());
                }
                if let Some(m) = &session.metrics {
                    eprintln!("metrics: {}", m.summary());
                }
            }
            Err(e) => {
                eprintln!("export failed: {e}");
                std::process::exit(3);
            }
        }
        return;
    }
    if let Err(e) = app::run(session, args.screenshot.clone()) {
        eprintln!("viewer: {e}");
        std::process::exit(4);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn family_hints_parse_and_reject_bad_forms() {
        let m = parse_family_hints(&["ugv-01=ugv".into(), "p=fixed_wing".into()]).expect("parses");
        assert_eq!(m.get("ugv-01"), Some(&Family::Ugv));
        assert_eq!(m.get("p"), Some(&Family::FixedWing));
        assert!(parse_family_hints(&["nope".into()]).is_err());
        assert!(parse_family_hints(&["a=rotor".into()]).is_err());
    }

    #[test]
    fn explicit_profiles_root_must_have_public_dir() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../profiles");
        assert!(find_profiles_root(Some(&root)).is_ok());
        assert!(find_profiles_root(Some(Path::new("/definitely/not/here"))).is_err());
    }

    #[test]
    fn cli_parses_export_and_screenshot_flags() {
        let a = Args::try_parse_from([
            "musubi-reference-viewer",
            "--in",
            "x",
            "--export",
            "y",
            "--asset-family",
            "ugv-01=ugv",
        ])
        .expect("parses");
        assert_eq!(a.input, PathBuf::from("x"));
        assert_eq!(a.export, Some(PathBuf::from("y")));
        assert!(a.screenshot.is_none());
        assert!(Args::try_parse_from(["musubi-reference-viewer"]).is_err());
    }
}
