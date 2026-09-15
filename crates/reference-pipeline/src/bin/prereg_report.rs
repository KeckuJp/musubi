#![allow(clippy::doc_markdown, clippy::print_stdout)]

use std::collections::BTreeMap;

use musubi_reference_pipeline::prereg::report_text;
use musubi_reference_readers::eval::{AttributionWindow, CauseStamp, EvalConfig};
use musubi_reference_types::ChannelId;

const USAGE: &str = "usage: musubi-reference-prereg-report [--seed N] [--t-max-ms N] \
[--attribution-window inject_end_plus_t_max|inject_start_plus_t_max] \
[--cause-stamp window_end|window_start] [--g-eff CHANNEL=MS ...]";

fn main() -> std::process::ExitCode {
    let mut seed = 42u64;
    let mut cfg = EvalConfig::default();
    let mut overrides: BTreeMap<ChannelId, u64> = BTreeMap::new();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0usize;
    while i < args.len() {
        let a = args[i].as_str();
        let next = |i: &mut usize| -> Option<String> {
            *i += 1;
            args.get(*i).cloned()
        };
        let bad = |what: &str| {
            eprintln!("{USAGE}\nbad value for {what}");
            std::process::ExitCode::from(2)
        };
        match a {
            "--seed" => match next(&mut i).and_then(|v| v.parse().ok()) {
                Some(v) => seed = v,
                None => return bad("--seed"),
            },
            "--t-max-ms" => match next(&mut i).and_then(|v| v.parse().ok()) {
                Some(v) => cfg.t_max_ms = v,
                None => return bad("--t-max-ms"),
            },
            "--attribution-window" => match next(&mut i).as_deref() {
                Some("inject_end_plus_t_max") => {
                    cfg.attribution_window = AttributionWindow::InjectEndPlusTMax;
                }
                Some("inject_start_plus_t_max") => {
                    cfg.attribution_window = AttributionWindow::InjectStartPlusTMax;
                }
                _ => return bad("--attribution-window"),
            },
            "--cause-stamp" => match next(&mut i).as_deref() {
                Some("window_end") => cfg.cause_stamp = CauseStamp::WindowEnd,
                Some("window_start") => cfg.cause_stamp = CauseStamp::WindowStart,
                _ => return bad("--cause-stamp"),
            },
            "--g-eff" => {
                let Some(spec) = next(&mut i) else {
                    return bad("--g-eff");
                };
                let Some((ch, ms)) = spec.split_once('=') else {
                    return bad("--g-eff (CHANNEL=MS)");
                };
                let (Some(channel), Ok(ms)) = (ChannelId::parse(ch), ms.parse::<u64>()) else {
                    return bad("--g-eff (unknown channel or non-numeric ms)");
                };
                overrides.insert(channel, ms);
            }
            "-h" | "--help" => {
                println!("{USAGE}");
                return std::process::ExitCode::SUCCESS;
            }
            other => {
                eprintln!("{USAGE}\nunknown argument {other}");
                return std::process::ExitCode::from(2);
            }
        }
        i += 1;
    }
    print!("{}", report_text(seed, &cfg, &overrides));
    std::process::ExitCode::SUCCESS
}
