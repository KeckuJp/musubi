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

pub mod export;
pub mod load;
pub mod prereg;
pub mod timefmt;

use std::collections::BTreeMap;
use std::path::Path;

use musubi_reference_readers::absence::{GapBounds, detect_absences_in_declared_window};
use musubi_reference_readers::catalog::load_catalog;
use musubi_reference_readers::cause::{
    SourceInput, Window, build_claim, event_trigger_times, windows_from_triggers,
};
use musubi_reference_readers::eval::{ClaimRecord, Incident, Metrics, NoticedAbsence, evaluate};
use musubi_reference_readers::profile::load_profiles;
use musubi_reference_readers::time_align::{
    align_by_start, estimate_alignment, estimate_relative_rate, promote, to_wall_us,
};
use musubi_reference_readers::{
    ClockBasis, FamilyProfile, NegativeObservation, Observation, reader_for,
};
use musubi_reference_scenario::{Family, GroundTruth, Timeline};
use musubi_reference_types::{CauseClaim, ChannelId, FailureKind, SignatureCatalog, SourceRole};
use musubi_reference_writers::{FamilyWriter, RenderedFile, default_writers, render_all};

pub use musubi_reference_readers::eval::ClaimRecord as Claim;

pub const ALIGN_BY_START_BOUND_US: i64 = 10_000_000;
pub const WINDOW_GAP_MS: i64 = 10_000;
pub const WINDOW_SPAN_MS: i64 = 30_000;
const EVENT_TRIGGER: usize = usize::MAX;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InputFile {
    pub asset_id: String,
    pub family: Family,
    pub source: SourceRole,
    pub format_id: String,
    pub file_name: String,
    pub bytes: Vec<u8>,
}

impl From<RenderedFile> for InputFile {
    fn from(f: RenderedFile) -> Self {
        Self {
            asset_id: f.asset_id,
            family: f.family,
            source: f.source,
            format_id: f.format_id.to_string(),
            file_name: f.file_name,
            bytes: f.bytes,
        }
    }
}

#[derive(Debug, Clone)]
pub struct SourceRun {
    pub profile: FamilyProfile,
    pub file_name: String,
    pub obs: Vec<Observation>,
    pub negatives: Vec<NegativeObservation>,
    pub gap_bounds: Vec<GapBounds>,
    pub alignment: musubi_reference_types::TimeAlignment,
    pub clock_rate: Option<musubi_reference_types::ClockRateReport>,
}

impl SourceRun {
    #[must_use]
    pub fn host_axis(&self) -> bool {
        matches!(
            self.profile.default_clock_basis,
            ClockBasis::HostReceived | ClockBasis::Unknown
        )
    }

    #[must_use]
    pub fn to_wall_ms(&self, native_ms: i64) -> Option<(i64, i64)> {
        if self.host_axis() {
            return Some((native_ms, 0));
        }
        let (w_us, b_us) = to_wall_us(&self.alignment, (native_ms.max(0) as u64) * 1000)?;
        Some((w_us / 1000, b_us / 1000))
    }
}

#[derive(Debug, Clone)]
pub struct AssetRun {
    pub asset_id: String,
    pub family: Family,
    pub sources: Vec<SourceRun>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NoticedRow {
    pub asset_id: String,
    pub source_index: usize,
    pub negative: NegativeObservation,
    pub since_wall_ms: i64,
    pub by_wall_ms: i64,
    pub bound_ms: i64,
    pub bounds: GapBounds,
}

pub const WALL_MAPPED: &str = "mapped";
pub const WALL_UNMAPPED_NO_TIME_OFFSET: &str = "unmapped_no_time_offset";
pub const WALL_UNMAPPED_NO_WALL_VALUE: &str = "unmapped_no_wall_value";

#[derive(Debug, Clone, PartialEq)]
pub struct UnmappedRow {
    pub asset_id: String,
    pub source_index: usize,
    pub negative: NegativeObservation,
    pub bounds: GapBounds,
    pub reason: &'static str,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObservationWindow {
    Explicit { t0_ms: i64, end_ms: i64 },
    Derived,
}

#[derive(Debug, Clone)]
pub struct PipelineOut {
    pub files: Vec<InputFile>,
    pub assets: Vec<AssetRun>,
    pub noticed: Vec<NoticedRow>,
    pub unmapped: Vec<UnmappedRow>,
    pub claims: Vec<ClaimRecord>,
    pub t0_ms: i64,
    pub end_ms: i64,
}

#[derive(Debug, Clone)]
pub struct Knowledge {
    pub profiles: Vec<FamilyProfile>,
    pub catalog: SignatureCatalog,
}

impl Knowledge {
    pub fn load(root: &Path) -> Result<Self, String> {
        let public = root.join("public");
        let partner = root.join("partner");
        let partner_opt = partner.is_dir().then_some(partner.as_path());
        let profiles = load_profiles(&public, partner_opt).map_err(|e| e.to_string())?;
        if profiles.is_empty() {
            return Err(format!("no profiles found under {}", public.display()));
        }
        let partner_cat = partner.join("catalog/failure_signatures.toml");
        let catalog = load_catalog(
            &public.join("catalog/failure_signatures.toml"),
            partner_cat.is_file().then_some(partner_cat.as_path()),
        )
        .map_err(|e| e.to_string())?;
        Ok(Self { profiles, catalog })
    }

    #[must_use]
    pub fn repo_public() -> Self {
        let public = repo_root().join("profiles/public");
        let profiles =
            load_profiles(&public, None).unwrap_or_else(|e| panic!("repo profiles: {e}"));
        let catalog = load_catalog(&public.join("catalog/failure_signatures.toml"), None)
            .unwrap_or_else(|e| panic!("repo catalog: {e}"));
        Self { profiles, catalog }
    }

    #[must_use]
    pub fn catalog_kinds(&self) -> BTreeMap<String, FailureKind> {
        self.catalog
            .signatures
            .iter()
            .map(|s| (s.signature_id.clone(), s.kind))
            .collect()
    }
}

#[must_use]
pub fn repo_root() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn profile_for<'a>(
    profiles: &'a [FamilyProfile],
    f: &InputFile,
) -> Result<&'a FamilyProfile, String> {
    let describe = || {
        format!(
            "{} (format {} / family {})",
            f.file_name,
            f.format_id,
            f.family.as_str()
        )
    };
    let matching: Vec<&FamilyProfile> = profiles
        .iter()
        .filter(|p| p.format == f.format_id && p.family == f.family)
        .collect();
    let first = *matching.first().ok_or_else(|| {
        format!(
            "no profile for {} (format {} / family {})",
            f.file_name,
            f.format_id,
            f.family.as_str()
        )
    })?;
    if matching.len() == 1
        || !matching
            .iter()
            .any(|p| p.declared_platform_domain.is_some() || p.declared_sender.is_some())
    {
        return Ok(first);
    }
    let by_role: Vec<&&FamilyProfile> = matching
        .iter()
        .filter(|p| p.source_role == f.source)
        .collect();
    if let [only] = by_role.as_slice() {
        return Ok(**only);
    }
    let ids: Vec<&str> = matching.iter().map(|p| p.profile_id.as_str()).collect();
    Err(format!(
        "ambiguous profile binding for {}: {} profiles match and at least one declares a \
         platform domain or a selected sender, so the source cannot be told apart by format, \
         family and source role alone ({})",
        describe(),
        matching.len(),
        ids.join(", ")
    ))
}

fn wall_to_axis(
    al: &musubi_reference_types::TimeAlignment,
    host_axis: bool,
    wall_ms: i64,
) -> Option<i64> {
    if host_axis {
        return Some(wall_ms);
    }
    let o = al.offset?;
    Some((wall_ms * 1000 - o.offset_us) / 1000)
}

pub fn analyze(
    files: Vec<InputFile>,
    knowledge: &Knowledge,
    window: ObservationWindow,
) -> Result<PipelineOut, String> {
    let profiles = &knowledge.profiles;
    let catalog = &knowledge.catalog;
    let mut assets_in: Vec<(String, Family)> = Vec::new();
    for f in &files {
        if !assets_in.iter().any(|(a, _)| a == &f.asset_id) {
            assets_in.push((f.asset_id.clone(), f.family));
        }
    }
    let mut read: Vec<Vec<(FamilyProfile, String, Vec<Observation>)>> = Vec::new();
    for (asset_id, _) in &assets_in {
        let mut pending = Vec::new();
        for f in files.iter().filter(|f| &f.asset_id == asset_id) {
            let p = profile_for(profiles, f)?;
            let reader = reader_for(&p.format)
                .ok_or_else(|| format!("no reader for format {}", p.format))?;
            let obs = reader
                .read(p, &f.bytes)
                .map_err(|e| format!("{}: {e}", f.file_name))?;
            pending.push((p.clone(), f.file_name.clone(), obs));
        }
        read.push(pending);
    }
    let host_obs = read
        .iter()
        .flatten()
        .filter(|(p, _, _)| matches!(p.default_clock_basis, ClockBasis::HostReceived))
        .flat_map(|(_, _, o)| o.iter().map(|x| x.t_ms));
    let (t0_ms, end_ms) = match window {
        ObservationWindow::Explicit { t0_ms, end_ms } => (t0_ms, end_ms),
        ObservationWindow::Derived => {
            let v: Vec<i64> = host_obs.collect();
            match (v.iter().min(), v.iter().max()) {
                (Some(a), Some(b)) => (*a, *b),
                _ => return Err("no host-axis observation to derive the window from".into()),
            }
        }
    };
    let mut assets: Vec<AssetRun> = Vec::new();
    for ((asset_id, family), pending) in assets_in.iter().zip(read) {
        let host_start = pending
            .iter()
            .filter(|(p, _, _)| matches!(p.default_clock_basis, ClockBasis::HostReceived))
            .flat_map(|(_, _, o)| o.iter().map(|x| x.t_ms))
            .min()
            .unwrap_or(t0_ms);
        let mut sources: Vec<SourceRun> = Vec::new();
        for (p, file_name, obs) in pending {
            let host_axis = matches!(
                p.default_clock_basis,
                ClockBasis::HostReceived | ClockBasis::Unknown
            );
            let mut al = estimate_alignment(&obs, p.default_clock_basis);
            if !host_axis && al.offset.is_none() {
                al = align_by_start(&obs, host_start, ALIGN_BY_START_BOUND_US).unwrap_or(al);
            }
            let obs = promote(&obs, &al);
            let (ws, we) = if host_axis {
                (t0_ms, end_ms)
            } else {
                let first = obs.iter().map(|o| o.t_ms).min().unwrap_or(0);
                let last = obs.iter().map(|o| o.t_ms).max().unwrap_or(0);
                (
                    wall_to_axis(&al, false, t0_ms).map_or(first, |v| v.min(first)),
                    wall_to_axis(&al, false, end_ms).map_or(last, |v| v.max(last)),
                )
            };
            let declared = if p.expectations.iter().any(|e| e.declared_window_only) {
                if matches!(p.default_clock_basis, ClockBasis::Unknown) {
                    return Err(format!(
                        "{} ({}): an expectation asks for the declared observation window, but \
                         this record's clock basis is unknown, so a wall-clock window cannot be \
                         placed on it; this feature does not give an unknown clock a duration it \
                         does not have",
                        p.profile_id, file_name
                    ));
                }
                match (
                    wall_to_axis(&al, host_axis, t0_ms),
                    wall_to_axis(&al, host_axis, end_ms),
                ) {
                    (Some(a), Some(b)) => (a, b),
                    _ => {
                        return Err(format!(
                            "{} ({}): an expectation asks for the declared observation window, but \
                             no time offset could be established for this boot-relative record, so \
                             the wall-clock window cannot be placed on its axis",
                            p.profile_id, file_name
                        ));
                    }
                }
            } else {
                (ws, we)
            };
            let (negatives, gap_bounds) =
                detect_absences_in_declared_window(&p, asset_id, &obs, (ws, we), declared)
                    .into_iter()
                    .unzip();
            let clock_rate = p
                .declared_clock_rate
                .map(|cfg| estimate_relative_rate(&obs, &al, &cfg));
            sources.push(SourceRun {
                profile: p,
                file_name,
                obs,
                negatives,
                gap_bounds,
                alignment: al,
                clock_rate,
            });
        }
        assets.push(AssetRun {
            asset_id: asset_id.clone(),
            family: *family,
            sources,
        });
    }
    let mut noticed: Vec<NoticedRow> = Vec::new();
    let mut unmapped: Vec<UnmappedRow> = Vec::new();
    let mut claims: Vec<ClaimRecord> = Vec::new();
    for a in &assets {
        let mut negs_wall: Vec<(i64, usize, usize)> = Vec::new(); // (since_wall, source idx, neg idx)
        for (si, s) in a.sources.iter().enumerate() {
            for (ni, n) in s.negatives.iter().enumerate() {
                let Some(((since, bound), (by, _))) = s
                    .to_wall_ms(n.absent_since_ms)
                    .zip(s.to_wall_ms(n.expected_by_ms))
                else {
                    unmapped.push(UnmappedRow {
                        asset_id: a.asset_id.clone(),
                        source_index: si,
                        negative: n.clone(),
                        bounds: s.gap_bounds[ni],
                        reason: if s.alignment.offset.is_none() {
                            WALL_UNMAPPED_NO_TIME_OFFSET
                        } else {
                            WALL_UNMAPPED_NO_WALL_VALUE
                        },
                    });
                    continue;
                };
                noticed.push(NoticedRow {
                    asset_id: a.asset_id.clone(),
                    source_index: si,
                    negative: n.clone(),
                    since_wall_ms: since,
                    by_wall_ms: by,
                    bound_ms: bound,
                    bounds: s.gap_bounds[ni],
                });
                negs_wall.push((since, si, ni));
            }
        }
        let mut triggers: Vec<(i64, usize)> = negs_wall
            .iter()
            .enumerate()
            .map(|(i, (t, _, _))| (*t, i))
            .collect();
        for s in &a.sources {
            for t in event_trigger_times(&s.obs) {
                triggers.push((t, EVENT_TRIGGER));
            }
        }
        let inputs: Vec<SourceInput<'_>> = a
            .sources
            .iter()
            .map(|s| SourceInput {
                profile: &s.profile,
                obs: &s.obs,
                negatives: &s.negatives,
                alignment: s.alignment,
            })
            .collect();
        for (w, ids) in windows_from_triggers(&triggers, WINDOW_GAP_MS, WINDOW_SPAN_MS) {
            let absences: Vec<&NegativeObservation> = ids
                .iter()
                .filter(|&&i| i != EVENT_TRIGGER)
                .map(|&i| {
                    let (_, si, ni) = negs_wall[i];
                    &a.sources[si].negatives[ni]
                })
                .collect();
            let trigger_channel = absences
                .first()
                .map_or(ChannelId::Event, |n| n.subject.channel);
            let claim = build_claim(
                catalog,
                &inputs,
                a.family,
                &a.asset_id,
                trigger_channel,
                &absences,
                w,
                w.end_ms,
            );
            claims.push(ClaimRecord {
                asset_id: a.asset_id.clone(),
                window_start_ms: w.start_ms,
                window_end_ms: w.end_ms,
                claim,
            });
        }
    }
    Ok(PipelineOut {
        files,
        assets,
        noticed,
        unmapped,
        claims,
        t0_ms,
        end_ms,
    })
}

#[must_use]
pub fn run_pipeline(tl: &Timeline) -> PipelineOut {
    let knowledge = Knowledge::repo_public();
    run_pipeline_with(tl, &knowledge)
}

#[must_use]
pub fn run_pipeline_with(tl: &Timeline, knowledge: &Knowledge) -> PipelineOut {
    let owned = default_writers();
    let writers: Vec<&dyn FamilyWriter> = owned.iter().map(AsRef::as_ref).collect();
    let assets_in: Vec<(String, Family)> = tl
        .assets
        .iter()
        .map(|a| (a.asset_id.clone(), a.family))
        .collect();
    let (files, _skipped) = render_all(tl, &assets_in, &writers);
    let t0_ms = tl.t0_unix_us / 1000;
    let end_ms = t0_ms + tl.duration_ms as i64;
    analyze(
        files.into_iter().map(InputFile::from).collect(),
        knowledge,
        ObservationWindow::Explicit { t0_ms, end_ms },
    )
    .unwrap_or_else(|e| panic!("pipeline: {e}"))
}

#[must_use]
pub fn claims_for<'a>(out: &'a PipelineOut, asset: &str) -> Vec<&'a CauseClaim> {
    out.claims
        .iter()
        .filter(|c| c.asset_id == asset)
        .map(|c| &c.claim)
        .collect()
}

#[must_use]
pub fn window_of(w: &Window) -> (i64, i64) {
    (w.start_ms, w.end_ms)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroundTruthRow {
    pub kind: FailureKind,
    pub family: Family,
    pub asset_id: String,
    pub t_start_ms: u64,
    pub t_end_ms: u64,
    pub affected_channels: Vec<ChannelId>,
    pub missing_channels: Vec<ChannelId>,
}

impl From<&GroundTruth> for GroundTruthRow {
    fn from(g: &GroundTruth) -> Self {
        Self {
            kind: g.kind,
            family: g.family,
            asset_id: g.asset_id.clone(),
            t_start_ms: g.t_start_ms,
            t_end_ms: g.t_end_ms,
            affected_channels: g.affected_channels.clone(),
            missing_channels: g.missing_channels.clone(),
        }
    }
}

#[must_use]
pub fn incidents_from(
    gt: &[GroundTruthRow],
    t0_ms: i64,
    profiles: &[FamilyProfile],
) -> Vec<Incident> {
    gt.iter()
        .map(|g| {
            let delay = profiles
                .iter()
                .filter(|p| p.family == g.family)
                .flat_map(|p| p.expectations.iter())
                .filter(|e| g.affected_channels.contains(&e.channel))
                .map(|e| (e.cadence_ms * u64::from(e.grace_k)) as i64)
                .min()
                .unwrap_or(3_000);
            Incident {
                asset_id: g.asset_id.clone(),
                family: g.family,
                kind: g.kind,
                t_inject_ms: t0_ms + g.t_start_ms as i64,
                t_end_ms: t0_ms + g.t_end_ms as i64,
                t_detectable_ms: t0_ms + g.t_start_ms as i64 + delay,
                affected_channels: g.affected_channels.clone(),
                indistinguishable: !g.missing_channels.is_empty(),
                channel_complete: g.missing_channels.is_empty(),
            }
        })
        .collect()
}

#[must_use]
pub fn incidents(tl: &Timeline, profiles: &[FamilyProfile]) -> Vec<Incident> {
    let rows: Vec<GroundTruthRow> = tl.ground_truth.iter().map(GroundTruthRow::from).collect();
    incidents_from(&rows, tl.t0_unix_us / 1000, profiles)
}

#[must_use]
pub fn metrics_from(
    gt: &[GroundTruthRow],
    t0_ms: i64,
    asset_count: usize,
    duration_ms: i64,
    out: &PipelineOut,
    knowledge: &Knowledge,
) -> Metrics {
    let inc = incidents_from(gt, t0_ms, &knowledge.profiles);
    let noticed: Vec<NoticedAbsence> = out
        .noticed
        .iter()
        .map(|n| NoticedAbsence::from_negative(&n.negative, n.since_wall_ms, n.by_wall_ms))
        .collect();
    evaluate(
        &inc,
        &noticed,
        &out.claims,
        &knowledge.catalog_kinds(),
        asset_count,
        duration_ms,
        30_000,
    )
}

#[must_use]
pub fn metrics_from_with(
    gt: &[GroundTruthRow],
    t0_ms: i64,
    asset_count: usize,
    duration_ms: i64,
    out: &PipelineOut,
    knowledge: &Knowledge,
    cfg: &musubi_reference_readers::eval::EvalConfig,
) -> Metrics {
    let inc = incidents_from(gt, t0_ms, &knowledge.profiles);
    let noticed: Vec<NoticedAbsence> = out
        .noticed
        .iter()
        .map(|n| NoticedAbsence::from_negative(&n.negative, n.since_wall_ms, n.by_wall_ms))
        .collect();
    musubi_reference_readers::eval::evaluate_with(
        &inc,
        &noticed,
        &out.claims,
        &knowledge.catalog_kinds(),
        asset_count,
        duration_ms,
        cfg,
    )
}

#[must_use]
pub fn metrics(tl: &Timeline, out: &PipelineOut) -> Metrics {
    let knowledge = Knowledge::repo_public();
    let rows: Vec<GroundTruthRow> = tl.ground_truth.iter().map(GroundTruthRow::from).collect();
    metrics_from(
        &rows,
        tl.t0_unix_us / 1000,
        tl.assets.len(),
        tl.duration_ms as i64,
        out,
        &knowledge,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_scenario::{generate, pre_demo_default};

    #[test]
    fn timeline_and_folder_paths_share_analyze_and_agree() {
        let tl = generate(&pre_demo_default(42));
        let a = run_pipeline(&tl);
        let dir =
            std::env::temp_dir().join(format!("musubi-reference-pipeline-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("mkdir");
        for f in &a.files {
            std::fs::write(dir.join(&f.file_name), &f.bytes).expect("write file");
        }
        let summary = format!(
            "musubi-reference-synth (P-03) seed=42 t0_unix_us={} duration_ms={} events=0 injections=0\n{}",
            tl.t0_unix_us,
            tl.duration_ms,
            tl.assets
                .iter()
                .map(|x| format!("asset {} family={}\n", x.asset_id, x.family.as_str()))
                .collect::<String>()
        );
        std::fs::write(dir.join("SUMMARY.txt"), summary).expect("write summary");
        let knowledge = Knowledge::repo_public();
        let loaded = load::load_dir(&dir, &knowledge.profiles, &BTreeMap::new()).expect("load dir");
        let b = analyze(loaded.files, &knowledge, loaded.window).expect("analyze");
        std::fs::remove_dir_all(&dir).ok();
        assert_eq!((a.t0_ms, a.end_ms), (b.t0_ms, b.end_ms));
        assert_eq!(a.noticed.len(), b.noticed.len());
        assert_eq!(a.claims.len(), b.claims.len());
        let key = |c: &ClaimRecord| (c.asset_id.clone(), c.window_start_ms);
        let mut ca = a.claims.clone();
        let mut cb = b.claims.clone();
        ca.sort_by_key(key);
        cb.sort_by_key(key);
        for (x, y) in ca.iter().zip(&cb) {
            assert_eq!(x.claim.claim_id, y.claim.claim_id);
            assert_eq!(x.claim.outcome, y.claim.outcome);
        }
    }
}
