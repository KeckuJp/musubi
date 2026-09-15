use std::collections::BTreeMap;
use std::path::Path;

use musubi_reference_readers::{FamilyProfile, profiles_for_extension, reader_for};
use musubi_reference_types::{ChannelId, FailureKind, Family};
use musubi_reference_writers::video::{NOMINAL_BYTES_PER_S, scan_dvr_dir};

use crate::{GroundTruthRow, InputFile, ObservationWindow};

#[derive(Debug, Clone)]
pub struct Loaded {
    pub files: Vec<InputFile>,
    pub window: ObservationWindow,
    pub t0_ms: Option<i64>,
    pub duration_ms: Option<i64>,
    pub ground_truth: Vec<GroundTruthRow>,
    pub skipped: Vec<(String, String)>,
    pub asset_family: BTreeMap<String, Family>,
}

#[must_use]
pub fn parse_summary(text: &str) -> (Option<i64>, Option<i64>, BTreeMap<String, Family>) {
    let mut t0 = None;
    let mut dur = None;
    let mut fam = BTreeMap::new();
    for line in text.lines() {
        let toks: Vec<&str> = line.split_whitespace().collect();
        if toks.first().copied() == Some("asset") && toks.len() >= 3 {
            let id = toks[1].to_string();
            if let Some(f) = toks
                .iter()
                .find_map(|t| t.strip_prefix("family="))
                .and_then(Family::parse)
            {
                fam.insert(id, f);
            }
        }
        for t in &toks {
            if let Some(v) = t.strip_prefix("t0_unix_us=") {
                t0 = v.parse::<i64>().ok().map(|us| us / 1000);
            } else if let Some(v) = t.strip_prefix("duration_ms=") {
                dur = v.parse::<i64>().ok();
            }
        }
    }
    (t0, dur, fam)
}

pub fn parse_ground_truth_csv(text: &str) -> Result<Vec<GroundTruthRow>, usize> {
    let mut out = Vec::new();
    let mut lines = text.lines().enumerate();
    let Some((_, header)) = lines.next() else {
        return Ok(out);
    };
    let cols: Vec<&str> = header.split(',').map(str::trim).collect();
    let idx = |name: &str| cols.iter().position(|c| *c == name).ok_or(0usize);
    let (ik, ifam, ia, is, ie, iaf) = (
        idx("kind")?,
        idx("family")?,
        idx("asset_id")?,
        idx("t_start_ms")?,
        idx("t_end_ms")?,
        idx("affected_channels")?,
    );
    let imiss = cols.iter().position(|c| *c == "missing_channels");
    for (ln, line) in lines {
        if line.trim().is_empty() {
            continue;
        }
        let c: Vec<&str> = line.split(',').map(str::trim).collect();
        let get = |i: usize| c.get(i).copied().ok_or(ln);
        let chans = |s: &str| -> Result<Vec<ChannelId>, usize> {
            s.split('|')
                .filter(|x| !x.is_empty())
                .map(|x| ChannelId::parse(x).ok_or(ln))
                .collect()
        };
        out.push(GroundTruthRow {
            kind: FailureKind::parse(get(ik)?).ok_or(ln)?,
            family: Family::parse(get(ifam)?).ok_or(ln)?,
            asset_id: get(ia)?.to_string(),
            t_start_ms: get(is)?.parse().map_err(|_| ln)?,
            t_end_ms: get(ie)?.parse().map_err(|_| ln)?,
            affected_channels: chans(get(iaf)?)?,
            missing_channels: match imiss {
                Some(i) => chans(c.get(i).copied().unwrap_or(""))?,
                None => Vec::new(),
            },
        });
    }
    Ok(out)
}

#[must_use]
pub fn split_stem(stem: &str) -> (String, String) {
    match stem.rsplit_once('_') {
        Some((a, l)) => (a.to_string(), l.to_string()),
        None => (stem.to_string(), String::new()),
    }
}

fn is_ignored_name(name: &str, ext: &str) -> bool {
    name == "SUMMARY.txt"
        || name == "ground_truth.csv"
        || name.starts_with('.')
        || matches!(ext, "toml" | "md" | "txt" | "png" | "json")
}

fn choose_profile<'a>(
    profiles: &'a [FamilyProfile],
    ext: &str,
    family: Option<Family>,
    bytes: &[u8],
    file_name: &str,
) -> Result<&'a FamilyProfile, String> {
    let mut cands: Vec<&FamilyProfile> = profiles_for_extension(profiles, ext);
    if let Some(f) = family {
        cands.retain(|p| p.family == f);
    }
    if cands.is_empty() {
        return Err(format!(
            "{file_name}: no profile accepts extension .{ext}{}",
            family.map_or(String::new(), |f| format!(" for family {}", f.as_str()))
        ));
    }
    if cands.len() == 1 {
        return Ok(cands[0]);
    }
    let readable: Vec<&FamilyProfile> = cands
        .iter()
        .copied()
        .filter(|p| {
            reader_for(&p.format).is_some_and(|r| r.read(p, bytes).is_ok_and(|o| !o.is_empty()))
        })
        .collect();
    match readable.len() {
        1 => Ok(readable[0]),
        0 => Err(format!(
            "{file_name}: none of the candidate profiles can read it: {}",
            cands
                .iter()
                .map(|p| p.profile_id.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        )),
        _ => Err(format!(
            "{file_name}: ambiguous profile ({}); pass --asset-family <asset>=<family> or a SUMMARY.txt with `asset <id> family=<f>`",
            readable
                .iter()
                .map(|p| format!("{} [{}]", p.profile_id, p.family.as_str()))
                .collect::<Vec<_>>()
                .join(", ")
        )),
    }
}

pub fn load_dir(
    dir: &Path,
    profiles: &[FamilyProfile],
    family_hints: &BTreeMap<String, Family>,
) -> Result<Loaded, String> {
    if !dir.is_dir() {
        return Err(format!("{} is not a directory", dir.display()));
    }
    let mut entries: Vec<_> = std::fs::read_dir(dir)
        .map_err(|e| format!("{}: {e}", dir.display()))?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .collect();
    entries.sort();
    let mut asset_family: BTreeMap<String, Family> = family_hints.clone();
    let (mut t0_ms, mut duration_ms) = (None, None);
    let summary = dir.join("SUMMARY.txt");
    if summary.is_file() {
        let text = std::fs::read_to_string(&summary).map_err(|e| format!("SUMMARY.txt: {e}"))?;
        let (t0, dur, fam) = parse_summary(&text);
        t0_ms = t0;
        duration_ms = dur;
        for (k, v) in fam {
            asset_family.entry(k).or_insert(v);
        }
    }
    let mut ground_truth = Vec::new();
    let gt = dir.join("ground_truth.csv");
    if gt.is_file() {
        let text = std::fs::read_to_string(&gt).map_err(|e| format!("ground_truth.csv: {e}"))?;
        ground_truth = parse_ground_truth_csv(&text)
            .map_err(|ln| format!("ground_truth.csv: bad row {ln}"))?;
    }
    let mut files: Vec<InputFile> = Vec::new();
    let mut skipped: Vec<(String, String)> = Vec::new();
    let mut dvr_dirs: Vec<(String, std::path::PathBuf)> = Vec::new();
    for p in entries {
        let name = p
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or_default()
            .to_string();
        if p.is_dir() {
            if let Some(asset) = name.strip_suffix("_dvr") {
                dvr_dirs.push((asset.to_string(), p.clone()));
            } else {
                skipped.push((name, "directory (not <asset>_dvr)".into()));
            }
            continue;
        }
        let ext = p
            .extension()
            .and_then(|x| x.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if ext.is_empty() || is_ignored_name(&name, &ext) {
            skipped.push((name, "not a record file".into()));
            continue;
        }
        let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or_default();
        let (asset, _label) = split_stem(stem);
        let bytes = std::fs::read(&p).map_err(|e| format!("{name}: {e}"))?;
        let fam = asset_family.get(&asset).copied();
        let prof = choose_profile(profiles, &ext, fam, &bytes, &name)?;
        asset_family.entry(asset.clone()).or_insert(prof.family);
        files.push(InputFile {
            asset_id: asset,
            family: prof.family,
            source: prof.source_role,
            format_id: prof.format.clone(),
            file_name: name,
            bytes,
        });
    }
    for (asset, dpath) in dvr_dirs {
        if files
            .iter()
            .any(|f| f.asset_id == asset && f.format_id == "video_presence")
        {
            skipped.push((
                format!("{asset}_dvr/"),
                "presence CSV present; directory not scanned".into(),
            ));
            continue;
        }
        let bytes =
            scan_dvr_dir(&dpath, NOMINAL_BYTES_PER_S).map_err(|e| format!("{asset}_dvr/: {e}"))?;
        let fam = asset_family.get(&asset).copied();
        let prof = choose_profile(profiles, "csv", fam, &bytes, &format!("{asset}_dvr/"))?;
        asset_family.entry(asset.clone()).or_insert(prof.family);
        files.push(InputFile {
            asset_id: asset.clone(),
            family: prof.family,
            source: prof.source_role,
            format_id: prof.format.clone(),
            file_name: format!("{asset}_dvr/ (scanned)"),
            bytes,
        });
    }
    if files.is_empty() {
        return Err(format!("{}: no readable record file", dir.display()));
    }
    let window = match (t0_ms, duration_ms) {
        (Some(t0), Some(d)) => ObservationWindow::Explicit {
            t0_ms: t0,
            end_ms: t0 + d,
        },
        _ => ObservationWindow::Derived,
    };
    Ok(Loaded {
        files,
        window,
        t0_ms,
        duration_ms,
        ground_truth,
        skipped,
        asset_family,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summary_yields_t0_duration_and_families() {
        let (t0, d, f) = parse_summary(
            "musubi-reference-synth (P-03) seed=42 t0_unix_us=1788166800000000 duration_ms=180000 events=7 injections=5\nasset ugv-01 family=ugv fc_log=ardupilot_bin link=analog_rf\nasset fpv-01 family=fpv\n",
        );
        assert_eq!(t0, Some(1_788_166_800_000));
        assert_eq!(d, Some(180_000));
        assert_eq!(f.get("ugv-01"), Some(&Family::Ugv));
        assert_eq!(f.get("fpv-01"), Some(&Family::Fpv));
    }

    #[test]
    fn ground_truth_csv_round_trips_the_synth_columns() {
        let rows = parse_ground_truth_csv(
            "kind,family,asset_id,t_start_ms,t_end_ms,affected_channels,prelude_ms,prelude_order,log_end_mode,handset_loss_mode,radio_status,link_profile,missing_channels\n\
             telemetry_link_loss,ugv,ugv-01,60000,90000,tlog|heartbeat,10000,noise_limited,truncated,zero_with_gps_blank,stops,analog_rf,\n\
             rc_link_loss,fpv,fpv-01,70000,90000,link_stats|rc|event,0,noise_limited,truncated,frozen,stops,analog_rf,link_stats\n",
        )
        .expect("parses");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].kind, FailureKind::TelemetryLinkLoss);
        assert_eq!(
            rows[0].affected_channels,
            vec![ChannelId::Tlog, ChannelId::Heartbeat]
        );
        assert!(rows[0].missing_channels.is_empty());
        assert_eq!(rows[1].missing_channels, vec![ChannelId::LinkStats]);
        assert!(parse_ground_truth_csv("kind,family\nzzz,ugv\n").is_err());
    }

    #[test]
    fn stem_splits_on_last_underscore() {
        assert_eq!(split_stem("ugv-01_gcs"), ("ugv-01".into(), "gcs".into()));
        assert_eq!(
            split_stem("fpv_x_blackbox"),
            ("fpv_x".into(), "blackbox".into())
        );
        assert_eq!(split_stem("solo"), ("solo".into(), String::new()));
    }

    #[test]
    fn unreadable_bytes_fail_loud_and_family_hint_resolves_extension_clash() {
        let k = crate::Knowledge::repo_public();
        let err =
            choose_profile(&k.profiles, "tlog", None, b"", "x_gcs.tlog").expect_err("unreadable");
        assert!(err.contains("x_gcs.tlog") && err.contains("none of the candidate profiles"));
        let p = choose_profile(&k.profiles, "tlog", Some(Family::Ugv), b"", "x_gcs.tlog")
            .expect("family resolves");
        assert_eq!(p.profile_id, "ardupilot_rover_tlog");
    }

    #[test]
    fn real_tlog_without_family_is_ambiguous_between_rover_and_plane_and_lists_both() {
        use musubi_reference_scenario::{generate, pre_demo_default};
        use musubi_reference_writers::{FamilyWriter, tlog::TlogWriter};
        let k = crate::Knowledge::repo_public();
        let tl = generate(&pre_demo_default(9));
        let bytes = TlogWriter.render(&tl, "ugv-01").expect("tlog");
        let err = choose_profile(&k.profiles, "tlog", None, &bytes, "ugv-01_gcs.tlog")
            .expect_err("ambiguous");
        assert!(err.contains("ambiguous profile"), "{err}");
        assert!(
            err.contains("ardupilot_rover_tlog") && err.contains("ardupilot_plane_tlog"),
            "{err}"
        );
        assert!(err.contains("--asset-family"));
        let p = choose_profile(
            &k.profiles,
            "tlog",
            Some(Family::FixedWing),
            &bytes,
            "ugv-01_gcs.tlog",
        )
        .expect("family resolves");
        assert_eq!(p.profile_id, "ardupilot_plane_tlog");
    }
}
