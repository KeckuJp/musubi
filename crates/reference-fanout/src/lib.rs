//! Offline reference observations to CoT, NVG and two CSV sinks.
//! Failed and withheld outputs remain accounted for; no network transport is included.

// # なぜ: 以下の pedantic/nursery lint は本 crate では advisory noise になる（workspace 既定は warn）。
// - cast_*: ms／µs／degE7 の変換は値域が有界（Unix ms・boot µs・±90/±180 度）。
// - doc_markdown: 日本語 doc 中の技術語を全部 backtick で囲む規律は採らない。
// - too_many_lines／missing_const_for_fn／module_name_repetitions: 骨格の読みやすさを優先。
#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::too_many_lines,
    clippy::missing_const_for_fn,
    clippy::format_collect,
    clippy::module_name_repetitions
)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

pub mod csvsink;

use std::collections::BTreeMap;

use musubi_core::seal_digest;
use musubi_northbound::NorthboundPublisher;
use musubi_northbound::fanout::{FanOutReport, HumanLane, fan_out};
use musubi_reference_pipeline::{AssetRun, PipelineOut, SourceRun};
use musubi_reference_readers::{FieldValue, Observation};
use musubi_reference_types::{
    CauseOutcome, ClockBasis, Family, NegativeObservation, join_reason_tokens,
};
use musubi_types::{
    Claim, ComObject, ConfidenceBasis, EvidenceEnvelope, Mark, MarkStatus, PlatformDomain,
    PlatformState, Position, Timestamps,
};

/// 観測 step を刻む間隔（ms）。demo で marker が動くだけの粗さ・golden を小さく保つ。
pub const OBSERVATION_STRIDE_MS: i64 = 30_000;

/// Observation-step staleness threshold: `age_ms / expected_cadence_ms > k`.
/// [`freshness_of`] applies this declared rule to the supplied timestamps and cadence.
/// It does not authenticate their clock basis.
pub const STALENESS_K: f64 = 2.0;

/// (age_ms, cadence_ms, stale)。`at_wall_ms` は step の時刻、`observed_at_wall_ms` は
/// その位置が実際に観測された時刻（どちらも host 軸 Unix ms）。
#[must_use]
pub fn freshness_of(
    at_wall_ms: i64,
    observed_at_wall_ms: i64,
    cadence_ms: i64,
) -> (i64, i64, bool) {
    let cadence = cadence_ms.max(1);
    let age = (at_wall_ms - observed_at_wall_ms).max(0);
    (age, cadence, (age as f64) / (cadence as f64) > STALENESS_K)
}

/// absence／cause marker の uid 接頭辞（asset marker と衝突させない・contract §3.2 の「別 uid」案）。
const UID_ABSENCE: &str = "#absence:";
const UID_CAUSE: &str = "#cause:";

/// fan-out に出す 1 件の種別（並び順は `Observation` → `Absence` → `Cause`）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum StepKind {
    /// 機体の位置観測（MARK は観測の鮮度から）。
    Observation,
    /// 期待 cadence に対する不在（`NegativeObservation`）。
    Absence,
    /// 窓の原因**候補**（`CauseClaim`・verdict ではない）。
    Cause,
}

impl StepKind {
    /// 書き出し／NDJSON に出す安定ラベル。
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Observation => "observation",
            Self::Absence => "absence",
            Self::Cause => "cause",
        }
    }
}

/// fan-out の 1 件（`PipelineOut` の 1 事象を、方言に依らない形で持つ）。
///
/// `envelope` が **単一 sealed 正本**（`seal_digest` 済・各 sink はこれを書き直さない）。
/// 残りの field は sink が方言ネイティブ欄へ落とすための素材で、envelope の内容と矛盾しない。
#[derive(Debug, Clone)]
pub struct DemoStep {
    /// 決定論の通し番号（0 始まり・並べ替え後に採番）。
    pub seq: usize,
    pub kind: StepKind,
    pub asset_id: String,
    pub family: Family,
    /// この step の時刻（host 軸 Unix ms）。
    pub at_wall_ms: i64,
    /// 観測そのものの時刻（host 軸 Unix ms）。absence／cause は「最後の良い観測」の時刻。
    pub observed_at_wall_ms: i64,
    /// 期待 cadence（ms・NDJSON `freshness.expected_cadence_ms`）。
    pub expected_cadence_ms: i64,
    /// この step の時刻軸が source の自己申告か（boot 相対等）。
    pub self_asserted_time: bool,
    /// CoT `uid` / NVG `uri` になる marker 名（asset とは別 uid の absence／cause marker を含む）。
    pub marker_id: String,
    /// 同一 asset の観測をまとめる id（NDJSON `lineage_group_id`）。
    pub lineage_group_id: String,
    /// 同一 lineage 内の描画スロット（NDJSON `distinct_render_slot`・merge しない）。
    pub distinct_render_slot: usize,
    /// 人が読む 1 行（CSV／runbook 用。CoT remarks の内容と同じ意味）。
    pub headline: String,
    /// sealed Evidence 正本。
    pub envelope: EvidenceEnvelope,
}

impl DemoStep {
    /// この step の MARK（envelope 由来＝sink 間で一致することを golden が固定する）。
    #[must_use]
    pub fn mark(&self) -> &Mark {
        &self.envelope.claim.mark
    }

    /// この step の位置（envelope 由来）。
    #[must_use]
    pub fn position(&self) -> Option<&Position> {
        match &self.envelope.observation {
            ComObject::PlatformState(ps) => ps.position.as_ref(),
            ComObject::HostStateObservation(_) => None,
        }
    }

    /// content digest の先頭 8 hex（書き出し／runbook の突き合わせ用）。
    #[must_use]
    pub fn digest8(&self) -> String {
        self.envelope
            .content_digest
            .map_or_else(|| "-".to_string(), |d| hex8(&d))
    }
}

/// digest の先頭 4 byte を hex 8 文字に（reference の書き出しと同じ見せ方）。
#[must_use]
pub fn hex8(digest: &[u8; 32]) -> String {
    digest[..4].iter().map(|b| format!("{b:02x}")).collect()
}

/// family → COM の物理ドメイン（CoT battle dimension／APcontractB SIDC の写像元）。
///
/// # なぜ: northbound は affiliation を判定しない（常に unknown）。ここで宣言するのは
/// **ドメインだけ**で、`Ugv` は地上、`FixedWing`／`Fpv` は空（`PlatformState.platform_domain`
/// は adapter 由来という既存規約に合わせ、reference では profile の family がその宣言元）。
///
/// 表そのものは [`musubi_reference_types::Family::platform_domain`] にあり、ここはその委譲
/// （2 つ目の family 表を作らないため）。既存の呼び出し・戻り値は変わらない。
#[must_use]
pub const fn domain_of(family: Family) -> PlatformDomain {
    family.platform_domain()
}

/// The domain one asset publishes, resolved **once** for the whole fan-out.
///
/// The family decides, exactly as before. Only while the family establishes nothing may
/// an explicit profile declaration establish a domain, so a family-established Ground or
/// Air is never overridden by a caller's configuration. If the asset's sources declare
/// *different* domains the caller has contradicted itself, so nothing is established and
/// the family answer stands — one of two conflicting declarations is never picked.
///
/// Configured either way: this resolves what the profile says, never what was observed.
#[derive(Debug, Clone)]
struct AssetDomain {
    domain: PlatformDomain,
    /// Asset-wide explanation, appended to **every** step that inherits this domain.
    ///
    /// Empty when the family established the domain, so an asset without a declaration
    /// keeps exactly the provenance it had. When a declaration is involved the lines say
    /// so and name the declaring profiles, because a step whose position or absence came
    /// from a different, non-declaring source still inherits the asset's domain and must
    /// not present it as if its own source had established it.
    provenance: Vec<String>,
}
fn asset_platform_domain(asset: &AssetRun) -> AssetDomain {
    let (domain, provenance) = asset.configured_platform_domain();
    AssetDomain { domain, provenance }
}

/// 記録形式ごとの位置 field（lat, lon, alt, 単位）。**推測しない**＝この表に無い形式は位置なし。
///
/// 単位は writer（`crates/reference-writers`）が実際に書いている形をそのまま写した:
/// MAVLink/ArduPilot/PX4 の GPS 系は degE7（int）、blackbox の `GPS_coord[]` と PX4 の
/// `vehicle_global_position` は十進度、EdgeTX の `GPS` 列は `"lat lon"` の文字列。
const POSITION_FIELDS: &[(&str, &str, Option<&str>, Scale)] = &[
    (
        "GLOBAL_POSITION_INT.lat",
        "GLOBAL_POSITION_INT.lon",
        Some("GLOBAL_POSITION_INT.alt"),
        Scale::E7Mm,
    ),
    ("GPS_RAW_INT.lat", "GPS_RAW_INT.lon", None, Scale::E7Mm),
    ("GPS.Lat", "GPS.Lng", Some("GPS.Alt"), Scale::E7Cm),
    (
        "sensor_gps.lat",
        "sensor_gps.lon",
        Some("sensor_gps.alt"),
        Scale::E7Mm,
    ),
    (
        "vehicle_global_position.lat",
        "vehicle_global_position.lon",
        Some("vehicle_global_position.alt"),
        Scale::DegM,
    ),
    ("GPS_coord[0]", "GPS_coord[1]", None, Scale::DegM),
];

/// 位置 field の単位。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Scale {
    /// 緯度経度 degE7（int）／高度 mm。
    E7Mm,
    /// 緯度経度 degE7（int）／高度 cm。
    E7Cm,
    /// 緯度経度 十進度／高度 m。
    DegM,
}

/// 1 観測から位置を取り出す（表に無い形式・値域外・Null Island は `None`＝でっち上げない）。
#[must_use]
fn position_of(obs: &Observation) -> Option<Position> {
    let get = |name: &str| -> Option<&FieldValue> {
        obs.fields.iter().find(|(n, _)| n == name).map(|(_, v)| v)
    };
    for (lat_f, lon_f, alt_f, scale) in POSITION_FIELDS {
        let (Some(la), Some(lo)) = (
            get(lat_f).and_then(FieldValue::as_f64),
            get(lon_f).and_then(FieldValue::as_f64),
        ) else {
            continue;
        };
        let div = if matches!(scale, Scale::DegM) {
            1.0
        } else {
            1e7
        };
        let alt = alt_f
            .and_then(get)
            .and_then(FieldValue::as_f64)
            .map(|v| match scale {
                Scale::E7Mm => v / 1000.0,
                Scale::E7Cm => v / 100.0,
                Scale::DegM => v,
            });
        return finite_position(la / div, lo / div, alt);
    }
    // EdgeTX は 1 列に `"lat lon"`（空欄＝GPS 不在）。
    if let Some(FieldValue::Text(s)) = get("GPS") {
        let mut it = s.split_whitespace();
        if let (Some(la), Some(lo)) = (it.next(), it.next())
            && let (Ok(la), Ok(lo)) = (la.parse::<f64>(), lo.parse::<f64>())
        {
            return finite_position(la, lo, None);
        }
    }
    None
}

/// A recorded position with the exact original observation, not a wall-clock step.
#[derive(Debug)]
pub struct RecordedPosition<'a> {
    pub asset_id: &'a str,
    pub source_index: usize,
    pub observation_index: usize,
    pub observation: &'a Observation,
    pub position: Position,
}

#[derive(Debug)]
pub struct RecordedPositions<'a> {
    pub observations: usize,
    /// Includes absent, invalid and unqualified positions; not proof of a fault.
    pub without_qualified_position: usize,
    pub positions: Vec<RecordedPosition<'a>>,
}

/// Reuse the qualified position resolver in asset/source/observation order.
/// No timestamp promotion, interpolation, sampling, wire emission or global-time sort.
#[must_use]
pub fn recorded_positions(out: &PipelineOut) -> RecordedPositions<'_> {
    let mut result = RecordedPositions {
        observations: 0,
        without_qualified_position: 0,
        positions: vec![],
    };
    for asset in &out.assets {
        for (source_index, source) in asset.sources.iter().enumerate() {
            for (observation_index, observation) in source.obs.iter().enumerate() {
                result.observations += 1;
                if let Some(position) = position_of(observation) {
                    result.positions.push(RecordedPosition {
                        asset_id: &asset.asset_id,
                        source_index,
                        observation_index,
                        observation,
                        position,
                    });
                } else {
                    result.without_qualified_position += 1;
                }
            }
        }
    }
    result
}

/// 値域と Null Island（writer が「GPS 無し」に使う 0,0）を弾く。
#[must_use]
fn finite_position(lat_deg: f64, lon_deg: f64, alt_m: Option<f64>) -> Option<Position> {
    if !lat_deg.is_finite()
        || !lon_deg.is_finite()
        || !(-90.0..=90.0).contains(&lat_deg)
        || !(-180.0..=180.0).contains(&lon_deg)
        || (lat_deg == 0.0 && lon_deg == 0.0)
    {
        return None;
    }
    Some(Position {
        lat_deg,
        lon_deg,
        alt_m: alt_m.filter(|a| a.is_finite()),
    })
}

/// asset の中で `at_wall_ms` 以前の最後の位置つき観測（無ければ以後の最初）。
///
/// 返すのは (位置, その観測の wall ms, **その位置を出した source の index**)。source index を返すのは、
/// 「どの記録から取った位置か」を来歴と clock_basis に正しく載せるため（別 source の clock 仮定を
/// 貼り付けない）。
#[must_use]
fn position_at(sources: &[SourceRun], at_wall_ms: i64) -> Option<(Position, i64, usize)> {
    let mut before: Option<(Position, i64, usize)> = None;
    let mut after: Option<(Position, i64, usize)> = None;
    for (si, s) in sources.iter().enumerate() {
        for o in &s.obs {
            let Some(w) = o.wall_ms else { continue };
            let Some(p) = position_of(o) else { continue };
            if w <= at_wall_ms {
                if before.as_ref().is_none_or(|(_, b, _)| w >= *b) {
                    before = Some((p, w, si));
                }
            } else if after.as_ref().is_none_or(|(_, a, _)| w < *a) {
                after = Some((p, w, si));
            }
        }
    }
    before.or(after)
}

/// `expectation_id`（`<id>@<profile version>` 形）に対応する期待 cadence（ms）。
///
/// # なぜ: NDJSON の `freshness.expected_cadence_ms` は「その channel が本来どれくらいの間隔で
/// 来るはずか」であって、profile 全体の最小 cadence ではない（分母を取り違えると `is_stale` が
/// 常に false になり、不在が「新鮮」に見えてしまう）。
#[must_use]
fn cadence_for_expectation(source: &SourceRun, expectation_id: &str) -> i64 {
    let base = expectation_id.split('@').next().unwrap_or(expectation_id);
    source
        .profile
        .expectations
        .iter()
        .find(|e| e.expectation_id == base)
        .map_or_else(|| min_cadence_ms(source), |e| (e.cadence_ms as i64).max(1))
}

/// この source の時刻軸が source の自己申告か（host 受領時刻でない＝boot 相対等）。
#[must_use]
fn self_asserted(basis: ClockBasis) -> bool {
    !matches!(basis, ClockBasis::HostReceived)
}

/// profile の期待 cadence の最小値（NDJSON `freshness.expected_cadence_ms` の分母）。
#[must_use]
fn min_cadence_ms(source: &SourceRun) -> i64 {
    source
        .profile
        .expectations
        .iter()
        .map(|e| e.cadence_ms as i64)
        .min()
        .unwrap_or(1_000)
        .max(1)
}

/// [`PipelineOut`] → fan-out の step 列（決定論・並びは時刻→asset→種別→marker）。
///
/// 3 種を出す:
/// - **観測**: asset ごとに `t0` から [`OBSERVATION_STRIDE_MS`] 刻み（marker が動く最小の demo）。
/// - **absence**: `NoticedRow` 1 件 = marker 1 つ（uid は `<asset>#absence:<channel>@<since>`）。
/// - **cause**: `ClaimRecord` 1 件 = marker 1 つ（uid は `<asset>#cause:<window start>`）。
#[must_use]
pub fn build_steps(out: &PipelineOut) -> Vec<DemoStep> {
    build_steps_with(out, 0)
}

/// [`build_steps`] の時刻シフト版（`shift_ms` を全時刻に足す）。
///
/// # なぜ: CoT の `stale` は `time + TTL`（既定 15 s）なので、scenario 絶対時刻（8/31）の
/// fixture をそのまま実 TAK client へ流すと **到着時点で stale 済み**になり、灰色化して数分で
/// 消える（contract §3「時刻の罠」）。8/27 の内部デモは near-now へずらして流す。ずらすのは
/// **build 時**なので、marker の ISO 文言・envelope の時刻・digest がすべて同じ時刻で整合する
/// （出力バイト列を後から文字列置換しない）。既定は `0`＝golden は scenario 絶対時刻のまま。
#[must_use]
pub fn build_steps_with(out: &PipelineOut, shift_ms: i64) -> Vec<DemoStep> {
    let mut steps: Vec<DemoStep> = Vec::new();
    let sh = |ms: i64| ms.saturating_add(shift_ms);
    for asset in &out.assets {
        let lineage = format!("lg-{}", asset.asset_id);
        // One resolution for every step this asset produces, so observation, absence and
        // cause markers can never disagree about the domain.
        let asset_domain = asset_platform_domain(asset);
        let cadence = asset
            .sources
            .iter()
            .map(min_cadence_ms)
            .min()
            .unwrap_or(1_000);
        // --- 観測 ---
        let mut t = out.t0_ms;
        while t <= out.end_ms {
            if let Some((pos, obs_ms, si)) = position_at(&asset.sources, t) {
                let source = asset.sources.get(si);
                let basis = source.map_or(ClockBasis::Unknown, |s| s.profile.default_clock_basis);
                // # なぜ: 分母は「位置を出した source 自身の期待 cadence」であって asset 全体の最小では
                // ない。別 source の速い cadence を借りると、2 Hz の GPS が 10 Hz 基準で stale 判定される。
                let cadence_src = source.map_or(cadence, min_cadence_ms);
                // **A-1 是正**: 以前はここで `MarkStatus::Ok` と `observation-fresh` を固定していた。
                // grid（30 s 刻み）は source が死んだ後も `position_at` が返す「最後の fix」を出し続けるので、
                // 固定 Ok だと **死んだ機体の古い位置が「新鮮」として MARK 無しの sink へ clean publish** され、
                // 本 crate が見せるはずの「運べない sink は WITHHELD」がその行だけ成立していなかった
                // （seed 42 の plane-01＝fc_failure で 4 行再現）。鮮度は共通の
                // `freshness_of` から導き、越えたら Degraded にする（＝legacy sink 側で自動的に WITHHELD）。
                let (age_ms, _, stale) = freshness_of(t, obs_ms, cadence_src);
                let last_seen = format!(
                    "last-observed:{}",
                    musubi_reference_pipeline::timefmt::iso_utc_ms(sh(obs_ms))
                );
                let age_tok = format!("age-ms:{age_ms}");
                // `position_at` は at-or-before が無いときだけ「以後の最初の fix」へ落ちる。その 1 件は
                // 時刻が step より後なので、黙って age 0（＝新鮮）に見せずに token で明示する。
                let from_later = obs_ms > t;
                let basis_tok = basis.reason_token();
                let mut reason = vec![
                    if stale {
                        "observation-stale"
                    } else {
                        "observation-fresh"
                    },
                    basis_tok.as_str(),
                    last_seen.as_str(),
                    age_tok.as_str(),
                ];
                if from_later {
                    reason.push("position-from-later-fix");
                }
                let mark = Mark {
                    status: if stale {
                        MarkStatus::Degraded
                    } else {
                        MarkStatus::Ok
                    },
                    reason_code: join_reason_tokens(&reason),
                    provenance: provenance_of(source, &asset.asset_id),
                };
                steps.push(step(
                    StepKind::Observation,
                    asset.asset_id.clone(),
                    asset.family,
                    &asset_domain,
                    sh(t),
                    sh(obs_ms),
                    cadence_src,
                    self_asserted(basis),
                    asset.asset_id.clone(),
                    lineage.clone(),
                    0,
                    if stale {
                        format!(
                            "{} last known position, {} s old at {} — the source stopped reporting",
                            asset.asset_id,
                            age_ms / 1000,
                            musubi_reference_pipeline::timefmt::iso_utc_ms(sh(t)),
                        )
                    } else {
                        format!(
                            "{} position at {} (from {} record{})",
                            asset.asset_id,
                            musubi_reference_pipeline::timefmt::iso_utc_ms(sh(t)),
                            asset.sources.len(),
                            if asset.sources.len() == 1 { "" } else { "s" }
                        )
                    },
                    pos,
                    mark,
                    // # なぜ: file 由来の観測に実測 confidence は無い。0.6 は adapter 既定値の慣行値で
                    // （`confidence_basis = AdapterAssigned` と対）、実測値ではないことを北向きの
                    // `BASIS:adapter-assigned` が明示する。absence は 0.0、cause は claim 由来。
                    0.6,
                ));
            }
            t += OBSERVATION_STRIDE_MS;
        }
        // --- absence ---
        for row in out.noticed.iter().filter(|n| n.asset_id == asset.asset_id) {
            let Some(source) = asset.sources.get(row.source_index) else {
                continue;
            };
            let Some((pos, _obs_ms, _si)) = position_at(&asset.sources, row.since_wall_ms) else {
                continue;
            };
            let mark = absence_mark(
                &row.negative,
                sh(row.since_wall_ms),
                sh(row.by_wall_ms),
                source,
            );
            // # なぜ: step の時刻は「Musubi が気づけた時刻」＝`expected_by`、観測時刻は「最後に
            // 来ていた時刻」＝`absent_since`。この 2 つを分けると NDJSON の
            // `freshness.age_ms = expected_by − absent_since` が不在の長さそのものになり、
            // `is_stale` が正しく立つ（同じ値を入れると不在が「新鮮」に見える）。
            steps.push(step(
                StepKind::Absence,
                asset.asset_id.clone(),
                asset.family,
                &asset_domain,
                sh(row.by_wall_ms),
                sh(row.since_wall_ms),
                cadence_for_expectation(source, &row.negative.expectation_id),
                self_asserted(row.negative.clock_basis),
                // # なぜ: source role を uid に含める。同じ asset の 2 つの記録（FC log と GCS log）が
                // 同じ channel の不在を同時刻に主張することがあり（plane-01 の gps_ekf）、uid が同じだと
                // 受信側 C2 で後の marker が前を上書きして「2 つの独立した主張」が 1 つに畳まれる。
                format!(
                    "{}{}{}.{}@{}",
                    asset.asset_id,
                    UID_ABSENCE,
                    row.negative.subject.channel.as_str(),
                    source.profile.source_role.as_str(),
                    sh(row.since_wall_ms)
                ),
                format!("{lineage}-g{}", sh(row.since_wall_ms)),
                // slot は後段の `assign_slots` が lineage group 単位で振り直す（ここは placeholder）。
                0,
                format!(
                    "{} channel {} absent since {} (expected by {}) — anchored on the last good position",
                    asset.asset_id,
                    row.negative.subject.channel.as_str(),
                    musubi_reference_pipeline::timefmt::iso_utc_ms(sh(row.since_wall_ms)),
                    musubi_reference_pipeline::timefmt::iso_utc_ms(sh(row.by_wall_ms)),
                ),
                pos,
                mark,
                0.0,
            ));
        }
        // --- cause ---
        for claim in out.claims.iter().filter(|c| c.asset_id == asset.asset_id) {
            let Some((pos, obs_ms, _si)) = position_at(&asset.sources, claim.window_start_ms)
            else {
                continue;
            };
            let mark = cause_mark(&claim.claim);
            steps.push(step(
                StepKind::Cause,
                asset.asset_id.clone(),
                asset.family,
                &asset_domain,
                sh(claim.window_start_ms),
                sh(obs_ms),
                cadence,
                claim.claim.input_confidence.time_confidence_min < 1.0,
                format!(
                    "{}{}{}",
                    asset.asset_id,
                    UID_CAUSE,
                    sh(claim.window_start_ms)
                ),
                format!("{lineage}-g{}", sh(claim.window_start_ms)),
                0,
                format!(
                    "{} cause candidate for the window at {} — {} (candidate, not a verdict)",
                    asset.asset_id,
                    musubi_reference_pipeline::timefmt::iso_utc_ms(sh(claim.window_start_ms)),
                    outcome_headline(&claim.claim.outcome),
                ),
                pos,
                mark,
                claim.claim.input_confidence.min_confidence,
            ));
        }
    }
    steps.sort_by(|a, b| {
        (a.at_wall_ms, &a.asset_id, a.kind, &a.marker_id).cmp(&(
            b.at_wall_ms,
            &b.asset_id,
            b.kind,
            &b.marker_id,
        ))
    });
    assign_slots(&mut steps);
    for (i, s) in steps.iter_mut().enumerate() {
        s.seq = i;
    }
    steps
}

/// 同一 lineage group の中で `distinct_render_slot` を 0,1,2… と振る（**merge しない**の担保）。
///
/// # なぜ: 同じ `lineage_group_id` を共有する主張に相異なる slot を付け、別の主張として保持する。
/// ここで 1 group ＝ 1 つの gap（同じ時刻に始まった absence 群＋その窓の cause 候補）
/// なので、group 内で slot が衝突すると renderer が 2 つの主張を 1 つに畳める余地が残る。
/// 観測 step は `lg-<asset>` の連続 frame（同じ物体の時系列）なので slot 0 のまま触らない。
fn assign_slots(steps: &mut [DemoStep]) {
    let mut counter: BTreeMap<String, usize> = BTreeMap::new();
    for s in steps.iter_mut() {
        if s.kind == StepKind::Observation {
            continue;
        }
        let n = counter.entry(s.lineage_group_id.clone()).or_insert(0);
        s.distinct_render_slot = *n;
        *n += 1;
    }
}

/// step 1 件を組み立て、envelope を `seal_digest` で封じる（fan-out 手前の単一正本）。
#[allow(clippy::too_many_arguments)]
fn step(
    kind: StepKind,
    asset_id: String,
    family: Family,
    asset_domain: &AssetDomain,
    at_wall_ms: i64,
    observed_at_wall_ms: i64,
    expected_cadence_ms: i64,
    self_asserted_time: bool,
    marker_id: String,
    lineage_group_id: String,
    distinct_render_slot: usize,
    headline: String,
    position: Position,
    mut mark: Mark,
    confidence: f32,
) -> DemoStep {
    // Every step that inherits the asset-wide domain also inherits the explanation of how
    // it was established, including steps whose own source declared nothing and steps
    // (cause) that have no single source at all. Assets without a declaration append
    // nothing, so existing provenance is untouched.
    mark.provenance
        .extend(asset_domain.provenance.iter().cloned());
    let ps = PlatformState {
        // # なぜ: COM の platform_id に合成 marker 名（`ugv-01#absence:rc.gcs@…`）が入る。
        // northbound は CoT `uid`／NVG `uri` をここから引くので、marker の同一性をここに置かないと
        // absence／cause を asset とは別の marker として出せない（contract §3.2 の別 uid 案）。
        // 機体そのものの id は先頭の `<asset_id>` 部分＝`DemoStep.asset_id` が正本。
        platform_id: marker_id.clone(),
        position: Some(position),
        mode: None,
        timestamps: Timestamps {
            // contract: observed_at は epoch **µs**、received_at は epoch **ms**（単位が違う）。
            observed_at: Some(observed_at_wall_ms.saturating_mul(1000)),
            received_at: at_wall_ms,
            time_confidence: if self_asserted_time { 0.5 } else { 1.0 },
        },
        // Resolved once per asset by `asset_platform_domain`: the family, unless it
        // establishes nothing and the profile declared one. Configured, never observed.
        platform_domain: asset_domain.domain,
    };
    let envelope = seal_digest(EvidenceEnvelope {
        observation: ComObject::PlatformState(ps),
        claim: Claim {
            claim_id: format!("{}:{}", kind.as_str(), marker_id),
            confidence,
            mark,
            // file 由来の観測に実測 confidence は無い＝adapter 既定値であることを明示する
            // （北向きの BASIS 欄が `adapter-assigned` になる・contractb-2）。
            confidence_basis: Some(ConfidenceBasis::AdapterAssigned),
        },
        classification: None,
        signature: None,
        content_digest: None,
        signer_id: None,
        signed_at: None,
        revocation_proof: None,
        trust_annotations: None,
    });
    DemoStep {
        seq: 0,
        kind,
        asset_id,
        family,
        at_wall_ms,
        observed_at_wall_ms,
        expected_cadence_ms,
        self_asserted_time,
        marker_id,
        lineage_group_id,
        distinct_render_slot,
        headline,
        envelope,
    }
}

/// source の来歴（CoT `PROV:` / NVG `modifiers` に出る）。
fn provenance_of(source: Option<&SourceRun>, asset_id: &str) -> Vec<String> {
    let mut out = vec![format!("musubi-reference/{asset_id}")];
    if let Some(s) = source {
        out.push(format!(
            "profile/{}@{}",
            s.profile.profile_id, s.profile.version
        ));
        out.push(format!("record/{}", s.file_name));
    }
    out
}

/// absence の MARK（既存 reason token を保ったまま `absent-since:` / `expected-by:` を足す）。
///
/// # なぜ: contract §3.2 は CoT remarks に `ABSENT_SINCE／EXPECTED_BY／CAUSE` を出したいと言うが、
/// northbound の remarks 文法（`MARK;CONF;PROV;BASIS` の 4 欄・`;` 区切り）は増やさない方針なので、
/// `reason_code` の `|` 連結 token として運ぶ（`;` を token に入れない＝contract のワイヤ文法契約）。
fn absence_mark(
    negative: &NegativeObservation,
    since_wall_ms: i64,
    by_wall_ms: i64,
    source: &SourceRun,
) -> Mark {
    let since = musubi_reference_pipeline::timefmt::iso_utc_ms(since_wall_ms);
    let by = musubi_reference_pipeline::timefmt::iso_utc_ms(by_wall_ms);
    let channel = format!("absent-channel:{}", negative.subject.channel.as_str());
    let since_tok = format!("absent-since:{since}");
    let by_tok = format!("expected-by:{by}");
    let exp = format!("expectation:{}", negative.expectation_id);
    let mut reason = vec![
        negative.mark.reason_code.as_str(),
        channel.as_str(),
        since_tok.as_str(),
        by_tok.as_str(),
        exp.as_str(),
    ];
    let last_good = negative
        .last_good
        .map(|d| format!("last-good:{}", hex8(&d.0)));
    if let Some(lg) = last_good.as_deref() {
        reason.push(lg);
    }
    let mut provenance = negative.mark.provenance.clone();
    provenance.extend(provenance_of(Some(source), &negative.subject.asset_id));
    Mark {
        status: negative.mark.status,
        reason_code: join_reason_tokens(&reason),
        provenance,
    }
}

/// cause 候補の MARK（`cause:` token を足す。**verdict とは書かない**）。
fn cause_mark(claim: &musubi_reference_types::CauseClaim) -> Mark {
    let cause = format!("cause:{}", outcome_token(&claim.outcome));
    let generator = format!("catalog:{}", claim.generator.catalog_version);
    let trigger = format!("trigger-channel:{}", claim.subject.channel.as_str());
    let reason = vec![
        claim.mark.reason_code.as_str(),
        trigger.as_str(),
        cause.as_str(),
        "candidate-not-verdict",
        generator.as_str(),
    ];
    let mut provenance = claim.mark.provenance.clone();
    provenance.push(format!("generator/{}", claim.generator.build_id));
    provenance.push(format!("claim/{}", claim.claim_id));
    Mark {
        status: claim.mark.status,
        reason_code: join_reason_tokens(&reason),
        provenance,
    }
}

/// cause 結果の短い token（reason_code に入れる・`;` を含めない）。
#[must_use]
pub fn outcome_token(outcome: &CauseOutcome) -> String {
    match outcome {
        // # なぜ: 順位は保存しない（contract §3-1）ので、先頭は「表示時 derive の代表」であって
        // ranking ではない。代替が何件あるかを必ず併記して単一候補に見せない。
        CauseOutcome::Consistent { candidates } => {
            let head = candidates
                .first()
                .map_or("none", |c| c.signature_id.as_str());
            format!("{head}+alt{}", candidates.len().saturating_sub(1))
        }
        CauseOutcome::Ambiguous { candidates, .. } => {
            let ids: Vec<&str> = candidates.iter().map(|c| c.signature_id.as_str()).collect();
            format!("ambiguous:{}", ids.join("/"))
        }
        CauseOutcome::Unknown { reason } => format!("unknown:{}", reason.as_label()),
    }
}

/// cause 結果の人が読む 1 行（CSV／runbook 用・3 状態を常に出す）。
#[must_use]
pub fn outcome_headline(outcome: &CauseOutcome) -> String {
    match outcome {
        CauseOutcome::Consistent { candidates } => {
            let head = candidates
                .first()
                .map_or("none", |c| c.signature_id.as_str());
            let alt = candidates.len().saturating_sub(1);
            format!(
                "consistent with {head} ({alt} alternative{}, shown without ranking)",
                if alt == 1 { "" } else { "s" }
            )
        }
        CauseOutcome::Ambiguous { candidates, .. } => format!(
            "ambiguous between {} signatures (no ranking)",
            candidates.len()
        ),
        CauseOutcome::Unknown { reason } => format!("unknown: {}", reason.as_label()),
    }
}

/// 1 step を全 sink へ fan-out した結果。
#[derive(Debug, Clone)]
pub struct StepReport {
    pub seq: usize,
    pub marker_id: String,
    pub kind: StepKind,
    pub report: FanOutReport,
}

/// 走行全体の集計（no-silent-drop の証跡）。
#[derive(Debug)]
pub struct FanOutRun {
    pub steps: Vec<StepReport>,
    /// WITHHELD envelope の受け皿（`FanOutReport::human_lane` を捨てない・contract）。
    pub human_lane: HumanLane,
    pub published: usize,
    pub withheld: usize,
    pub failed: usize,
    /// 人間判断レーンが容量超過で退避した件数（0 でないなら受け皿が小さすぎる＝黙って消さない）。
    pub evicted: usize,
}

/// step 列を **4 種の sink**（CoT／NVG／汎用 CSV／legacy 位置のみ CSV）へ
/// fan-out する。`fan_out` を 1 step 1 回呼ぶだけで、単一 sealed 正本は書き直さない。
///
/// 汎用 CSV の publisher は **step ごとに作る**（契約 field が envelope でなく step 由来
/// のため）。各 step の `human_lane`（WITHHELD された raw）は捨てずに [`FanOutRun::human_lane`]
/// へ積む（contract＝呼び出し側が捨てること自体が silent drop）。
#[must_use]
pub fn run(steps: &[DemoStep], _t0_ms: i64) -> FanOutRun {
    let cot = musubi_northbound::CotPublisher::new();
    let nvg = musubi_northbound::NvgPublisher::new();
    let legacy = csvsink::LegacyPositionCsvPublisher::new();
    let mut lane = HumanLane::new((steps.len().max(1)) * SINK_COUNT);
    let mut out = Vec::with_capacity(steps.len());
    let (mut published, mut withheld, mut failed) = (0, 0, 0);
    let mut evicted = 0usize;
    for s in steps {
        let csv = csvsink::EventCsvPublisher::new(s);
        let publishers: [&dyn NorthboundPublisher; SINK_COUNT] = [&cot, &nvg, &csv, &legacy];
        let report = fan_out(&s.envelope, &publishers);
        published += report.published_count();
        withheld += report.withheld_count();
        failed += report.failed_count();
        for (dialect, envelope) in &report.human_lane {
            // # なぜ: `push` の戻り値＝容量超過で退避された entry。捨てると contract が塞いだ
            // 「呼び出し側が黙って落とす」穴が戻る。容量は steps×sinks なので今日は発火しないが、
            // 発火したら件数として残す。
            if lane.push(envelope.clone(), dialect.clone()).is_some() {
                evicted += 1;
            }
        }
        out.push(StepReport {
            seq: s.seq,
            marker_id: s.marker_id.clone(),
            kind: s.kind,
            report,
        });
    }
    FanOutRun {
        steps: out,
        human_lane: lane,
        published,
        withheld,
        failed,
        evicted,
    }
}

/// dialect ごとに publish されたバイト列を **step 順に連結**する（stream 形の sink 用）。
///
/// WITHHELD／Failed は出力バイトが無いので何も足さない＝「その sink には出ていない」ことが
/// そのまま残る（台帳側 [`StepReport`] が理由を持つ）。書き出す bin と golden test が
/// **同じ関数**を通ることで、docs に載せた例と test が食い違わない。
#[must_use]
pub fn concat_by_dialect(report: &FanOutRun) -> BTreeMap<String, Vec<u8>> {
    let mut out: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    for step in &report.steps {
        for outcome in &step.report.outcomes {
            if let Some(bytes) = outcome.bytes() {
                out.entry(outcome.dialect().to_string())
                    .or_default()
                    .extend_from_slice(bytes);
            }
        }
    }
    out
}

/// fan-out する sink の数（CoT／NVG／汎用 CSV／legacy 位置のみ CSV）。
/// 同梱 viewer は同じ folder を読む 別の consumer で、この fan-out の外側にいる。
pub const SINK_COUNT: usize = 4;
