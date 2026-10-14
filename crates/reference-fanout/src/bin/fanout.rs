//! Write offline reference observations to files with per-sink accounting.
//! The optional replay time shift changes presentation timestamps, not source clock trust.

#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::doc_markdown,
    clippy::format_push_string,
    clippy::too_many_lines
)]

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use clap::Parser;
use musubi_northbound::fanout::SinkOutcome;
use musubi_reference_fanout::{build_steps_with, concat_by_dialect, csvsink, run};
use musubi_reference_pipeline::export::{export_all, export_all_with_declared_domain};
use musubi_reference_pipeline::load::load_dir;
use musubi_reference_pipeline::{Knowledge, analyze, metrics_from};
use musubi_reference_types::Family;

#[derive(Parser, Debug)]
#[command(name = "musubi-reference-fanout", version, about)]
struct Args {
    /// 入力 folder（`musubi-reference-synth` の出力の形）。
    #[arg(long = "in")]
    input: PathBuf,
    /// 出力 folder（ここにだけ書く）。
    #[arg(long = "out")]
    output: PathBuf,
    /// profile root（`<dir>/public/*.toml`＋任意の `<dir>/partner/*.toml`）。省略時は `./profiles`。
    #[arg(long)]
    profiles: Option<PathBuf>,
    /// file 名から family が決まらないときの明示指定: `ASSET=FAMILY`。
    #[arg(long = "asset-family")]
    asset_family: Vec<String>,
    /// 時刻を「今」へずらして出す（CoT の stale が到着時に切れているのを避ける・contract §3）。
    #[arg(long)]
    near_now: bool,
    /// `--near-now` の基準時刻を固定する（epoch ms・決定論が要るとき）。
    #[arg(long)]
    now_ms: Option<i64>,
    /// Add configured domain/basis/provenance to absence/cause CSV (schema 4).
    /// Configuration is not an observed or authenticated physical domain.
    #[arg(long)]
    include_declared_domain: bool,
}

fn family_hints(specs: &[String]) -> Result<BTreeMap<String, Family>, String> {
    let mut m = BTreeMap::new();
    for s in specs {
        let (a, f) = s
            .split_once('=')
            .ok_or_else(|| format!("bad --asset-family '{s}' (ASSET=FAMILY)"))?;
        m.insert(
            a.to_string(),
            Family::parse(f).ok_or_else(|| format!("unknown family '{f}'"))?,
        );
    }
    Ok(m)
}

fn profiles_root(explicit: Option<&Path>) -> Result<PathBuf, String> {
    let mut cands = Vec::new();
    if let Some(p) = explicit {
        cands.push(p.to_path_buf());
    } else {
        cands.push(PathBuf::from("profiles"));
        if let Ok(exe) = std::env::current_exe()
            && let Some(dir) = exe.parent()
        {
            cands.push(dir.join("profiles"));
            cands.push(dir.join("../../profiles"));
        }
    }
    for c in &cands {
        if c.join("public").is_dir() {
            return Ok(c.clone());
        }
    }
    Err(format!(
        "profiles root not found (tried: {}); pass --profiles <dir>",
        cands
            .iter()
            .map(|c| c.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    ))
}

/// marker id を file 名に使える形へ（`#`／`:` を `-` に）。
fn safe_name(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' {
                c
            } else {
                '-'
            }
        })
        .collect()
}

/// 現在時刻（epoch ms）。`--near-now` のときだけ読む＝既定経路は壁時計に触らない。
fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as i64)
}

fn main() {
    let args = Args::parse();
    if let Err(e) = real_main(&args) {
        eprintln!("musubi-reference-fanout: {e}");
        std::process::exit(2);
    }
}

fn real_main(args: &Args) -> Result<(), String> {
    let root = profiles_root(args.profiles.as_deref())?;
    let knowledge = Knowledge::load(&root)?;
    let hints = family_hints(&args.asset_family)?;
    let loaded = load_dir(&args.input, &knowledge.profiles, &hints)?;
    for (name, why) in &loaded.skipped {
        eprintln!("skipped {name}: {why}");
    }
    let out = analyze(loaded.files.clone(), &knowledge, loaded.window)?;
    let shift = if args.near_now {
        args.now_ms.unwrap_or_else(now_ms) - out.t0_ms
    } else {
        0
    };
    let steps = build_steps_with(&out, shift);
    let t0 = out.t0_ms + shift;
    let report = run(&steps, t0);
    let dir = &args.output;
    std::fs::create_dir_all(dir.join("cot")).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(dir.join("nvg")).map_err(|e| e.to_string())?;

    // stream 形の sink は golden test と同じ `concat_by_dialect` を通す（docs の例と test が
    // 食い違わないように、書き出しと検証で同じ関数を使う）。
    let streams = concat_by_dialect(&report);
    let take = |d: &str| streams.get(d).cloned().unwrap_or_default();
    // (ii) CoT: event を連結した TAK stream 形。
    let cot = take("cot");
    // (v) 汎用 CSV 2 本。
    let mut csv = format!("{}\n", csvsink::CSV_HEADER).into_bytes();
    csv.extend_from_slice(&take(csvsink::DIALECT_CSV));
    let mut legacy = format!("{}\n", csvsink::LEGACY_HEADER).into_bytes();
    legacy.extend_from_slice(&take(csvsink::DIALECT_LEGACY));
    // (iii) NVG: 1 document 1 file（NVG は連結しても 1 document にならない）。
    let mut ledger = String::from(
        "seq,kind,marker_id,dialect,outcome,mark_status,mark_reason_code,would_drop\n",
    );
    for (sr, step) in report.steps.iter().zip(&steps) {
        for outcome in &sr.report.outcomes {
            let (label, drop) = match outcome {
                SinkOutcome::Published { .. } => ("published", String::new()),
                SinkOutcome::PublishedWithDowngrade { carried, .. } => (
                    "published-with-visible-downgrade",
                    format!("carried:{}", carried.len()),
                ),
                SinkOutcome::Withheld { would_drop, .. } => (
                    "WITHHELD",
                    would_drop
                        .iter()
                        .map(|f| format!("{f:?}"))
                        .collect::<Vec<_>>()
                        .join("|"),
                ),
                SinkOutcome::Failed { error, .. } => ("failed", error.reason.clone()),
            };
            let mark = match outcome {
                SinkOutcome::Withheld { mark, .. } => mark.clone(),
                _ => step.mark().clone(),
            };
            ledger.push_str(&ledger_row(
                sr.seq,
                sr.kind.as_str(),
                &sr.marker_id,
                outcome.dialect(),
                label,
                &mark,
                &drop,
            ));
            if outcome.dialect() == "nvg"
                && let Some(bytes) = outcome.bytes()
            {
                let p = dir.join("nvg").join(format!(
                    "{:04}_{}.nvg.xml",
                    sr.seq,
                    safe_name(&sr.marker_id)
                ));
                std::fs::write(p, bytes).map_err(|e| e.to_string())?;
            }
        }
    }
    write_file(&dir.join("cot/stream.xml"), &cot)?;
    write_file(&dir.join("fanout_events.csv"), &csv)?;
    write_file(&dir.join("legacy_position.csv"), &legacy)?;
    write_file(&dir.join("fanout_ledger.csv"), ledger.as_bytes())?;

    // WITHHELD された raw の行き先（X-DEGRADE-2WAY・捨てない）。
    let mut lane = String::from("sequence,dialect,owner,marker_id\n");
    for e in report.human_lane.pending() {
        let observation_id = match &e.envelope.observation {
            musubi_types::ComObject::PlatformState(ps) => ps.platform_id.as_str(),
            musubi_types::ComObject::HostStateObservation(host) => host.host_id.as_str(),
        };
        lane.push_str(&format!(
            "{},{},{},{}\n",
            e.sequence,
            e.dialect,
            e.owner,
            csvsink::cell(observation_id)
        ));
    }
    write_file(&dir.join("human_lane.csv"), lane.as_bytes())?;

    // (v) 既存 export をそのまま再利用（contract §3-8 の列・unknown を落とさない）。
    let metrics = if loaded.ground_truth.is_empty() {
        None
    } else {
        Some(metrics_from(
            &loaded.ground_truth,
            loaded.t0_ms.unwrap_or(out.t0_ms),
            out.assets.len(),
            loaded.duration_ms.unwrap_or(out.end_ms - out.t0_ms),
            &out,
            &knowledge,
        ))
    };
    if args.include_declared_domain {
        export_all_with_declared_domain(&out, &knowledge, metrics.as_ref(), dir)
    } else {
        export_all(&out, &knowledge, metrics.as_ref(), dir)
    }
    .map_err(|e| e.to_string())?;

    eprintln!(
        "fan-out: {} steps x {} sinks -> published {} / withheld {} / failed {} (human lane holds {})",
        steps.len(),
        musubi_reference_fanout::SINK_COUNT,
        report.published,
        report.withheld,
        report.failed,
        report.human_lane.pending_len(),
    );
    eprintln!("wrote {}", dir.display());
    if shift != 0 {
        eprintln!("times shifted by {shift} ms (--near-now)");
    }
    Ok(())
}

/// `fanout_ledger.csv` の 1 行（`seq,kind,marker_id,dialect,outcome,mark_status,mark_reason_code,would_drop`）。
///
/// **`mark_status` は `fanout_events.csv`／contract の書き出しと同じ snake_case 語彙**（contract・contract C-2）＝
/// `musubi-reference-fanout` は 3 つの CSV を同じ folder へ書くので、同名の列が file によって別表記だと
/// 受け取った側が 1 つの folder の中で 2 つの語彙を覚えることになる。
///
/// `outcome` 列の `WITHHELD` は **表記統一の対象ではない**: MARK の語彙ではなく、デモで目で拾うための
/// 可視ラベル（`published` との対比が要点で、runbook の grep がこの綴りを引く）。`would_drop` の
/// `MarkStatus|ReasonCode|…` も同様に「消えたはずの trust-field の**名前**」であって status 値ではない。
fn ledger_row(
    seq: usize,
    kind: &str,
    marker_id: &str,
    dialect: &str,
    outcome_label: &str,
    mark: &musubi_types::Mark,
    would_drop: &str,
) -> String {
    format!(
        "{},{},{},{},{},{},{},{}\n",
        seq,
        kind,
        csvsink::cell(marker_id),
        dialect,
        outcome_label,
        musubi_reference_pipeline::export::mark_label(mark.status),
        csvsink::cell(&mark.reason_code),
        csvsink::cell(would_drop),
    )
}

fn write_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    std::fs::write(path, bytes).map_err(|e| format!("{}: {e}", path.display()))
}

/// step 数を数えるだけの補助（bin の test 用）。
#[cfg(test)]
fn step_kinds(steps: &[musubi_reference_fanout::DemoStep]) -> (usize, usize, usize) {
    use musubi_reference_fanout::StepKind;
    let c = |k: StepKind| steps.iter().filter(|s| s.kind == k).count();
    (
        c(StepKind::Observation),
        c(StepKind::Absence),
        c(StepKind::Cause),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn family_hints_parse_and_reject_bad_forms() {
        let m = family_hints(&["ugv-01=ugv".into()]).expect("parses");
        assert_eq!(m.get("ugv-01"), Some(&Family::Ugv));
        assert!(family_hints(&["nope".into()]).is_err());
        assert!(family_hints(&["a=rotor".into()]).is_err());
    }

    #[test]
    fn ledger_mark_status_uses_the_shared_snake_case_vocabulary() {
        // contract 回帰ガード: 台帳の `mark_status` が `{:?}` 由来 CamelCase に戻ったら落ちる。
        let mark = musubi_types::Mark {
            status: musubi_types::MarkStatus::Withheld,
            reason_code: "sink-cannot-carry-mark:legacy-position-csv".into(),
            provenance: vec![],
        };
        let row = ledger_row(
            10,
            "absence",
            "ugv-01#absence:heartbeat.gcs@1788166859000",
            "legacy-position-csv",
            "WITHHELD",
            &mark,
            "MarkStatus|ReasonCode|Confidence|Provenance",
        );
        let cols: Vec<&str> = row.trim_end().split(',').collect();
        assert_eq!(cols[5], "withheld", "mark_status must be snake_case: {row}");
        // `outcome` 列の可視ラベルは統一の対象ではない（そのまま残す）。
        assert_eq!(cols[4], "WITHHELD");
        assert!(!row.contains("Withheld"));
    }

    #[test]
    fn marker_ids_become_safe_file_names() {
        assert_eq!(safe_name("fpv-01#absence:rc@123"), "fpv-01-absence-rc-123");
    }

    #[test]
    fn cli_requires_in_and_out() {
        assert!(Args::try_parse_from(["musubi-reference-fanout"]).is_err());
        let a = Args::try_parse_from(["musubi-reference-fanout", "--in", "x", "--out", "y"])
            .expect("parses");
        assert_eq!(a.input, PathBuf::from("x"));
        assert!(!a.near_now);
        assert!(!a.include_declared_domain);
        let opted = Args::try_parse_from([
            "musubi-reference-fanout",
            "--in",
            "x",
            "--out",
            "y",
            "--include-declared-domain",
        ])
        .expect("explicit opt-in");
        assert!(opted.include_declared_domain);
    }

    #[test]
    fn seed_42_pipeline_yields_all_three_step_kinds() {
        let tl =
            musubi_reference_scenario::generate(&musubi_reference_scenario::pre_demo_default(42));
        let out = musubi_reference_pipeline::run_pipeline(&tl);
        let (obs, absence, cause) = step_kinds(&build_steps_with(&out, 0));
        assert!(
            obs > 0 && absence > 0 && cause > 0,
            "{obs}/{absence}/{cause}"
        );
    }
}
