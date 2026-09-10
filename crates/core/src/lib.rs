//! Musubi deterministic read-only core。
//!
//! 信頼できない現場機器の観測を COM へ正規化し、Evidence/MARK を付与する
//! **決定論パイプライン**（AI モデルを持たない）。本 crate が守る不変条件:
//! - `unsafe` を持たない（workspace lint `unsafe_code = "forbid"`）。
//! - 依存閉包に network crate を持たない（build gate が機械検査する）。
//! - 実行 endpoint を型として持たない（入力側は read-only tap・出力側は publish のみ）。

use musubi_types::{
    Claim, ComObject, EvidenceEnvelope, HostStateCounter, HostStateNetworkInterface,
    HostStateObservation, Mark, MarkStatus, PlatformDomain, Position, Timestamps,
};
use sha2::{Digest, Sha256};

// =====================================================================================
// DDIL 決定論機構（net-free・in-memory）。
// =====================================================================================
//
// 3 モジュールはすべて socket/net を持たず（実搬送は別プロセスの責務）・unsafe 無し・
// EvidenceEnvelope を変更しない（vector clock は sidecar・golden digest 不変）。
// - `vector_clock`: per-source 論理カウンタ＋因果比較（Concurrent が ORDER_UNKNOWN の根拠）。
// - `store_and_forward`: link 状態で滞留→復旧で因果順 drain・全件保存（no-silent-drop）。
// - `ddil`: 5 プロファイル（NOMINAL/DEGRADED/DENIED/INTERMITTENT/LIMITED）→ 挙動＋MARK 写像。
pub mod ddil;
pub mod store_and_forward;
pub mod vector_clock;

// =====================================================================================
// 非信頼・アダプタ由来入力の quarantine harness。詳細は `quarantine` モジュール doc。
// =====================================================================================
pub mod quarantine;

/// 南向きの **read-only** な生観測（媒介 egress tap から・1 バイトも注入しない）。
///
/// Day-1 は不透明バイト列＋source タグ。具体デコードは adapter crate が担う。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawObservation {
    pub source_id: String,
    pub payload: Vec<u8>,
    /// **ingest 層**（transport/read-only tap）がこの生観測を実際に受領した時刻（epoch **ミリ秒**）。
    ///
    /// `Timestamps.received_at` を adapter の正規化経路で `0` に決め打ちすると、二重時刻
    /// （observed_at／received_at）がワイヤ上で構造的に潰れる。よって
    /// **seal 前の ingest 層**（read-only tap の実受領時刻、あるいはスクリプト化シナリオの
    /// 疑似 ingest clock）がこの値を確定させ、adapter の `Normalizer::normalize` は
    /// `Timestamps.received_at` へ
    /// **そのまま転記する**（自分で `0` を決め打ちしない）。単位は `Timestamps.received_at` と
    /// 同じ epoch ms（`observed_at` は epoch µs で別契約＝`musubi_types::Timestamps` 参照）。
    pub received_at: i64,
}

/// 正規化失敗（一級オブジェクト＝「失敗を捨てない」）。
///
/// 黙って落とさず（no-silent-drop）、MARK 化して下流に渡す材料にする。
#[derive(Debug, Clone, PartialEq)]
pub struct NormalizeError {
    pub source_id: String,
    pub mark: Mark,
}

/// 正規化の契約: raw 観測 → COM を内包した Evidence Envelope。
///
/// 実装は決定論。`Normalizer` は **read のみ**を受け取り、書き戻し手段を持たない
/// （戻り値も Evidence＝観測の証拠であって、機体への指示ではない）。
pub trait Normalizer {
    /// raw 観測を Evidence Envelope へ正規化する。
    ///
    /// # Errors
    /// 入力が当該 source の既知 schema に適合しない場合に [`NormalizeError`] を返す。
    fn normalize(&self, raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError>;
}

// =====================================================================================
// MARK status 意味論 — 観測の完全性/品質から決定論で MARK を導出する。
// =====================================================================================
//
// 設計の核: status は **この node が付与する量**であって上流が名乗る値ではない。
// adapter は「観測の品質シグナル」（`ObservationQuality`）だけを組み立て、status/reason_code
// への写像は **core が一手に決める**（adapter ごとに語彙がブレない・no-silent-drop を core で担保）。
//
// ここで発火するのは OK / DEGRADED / INVALID の 3 値のみ。残り 4 値
// （ORDER_UNKNOWN=因果順不明 / WITHHELD=fan-out の出し分け / STALE_REVOCATION=失効 /
// SELF_ASSERTED_TIME=self-asserted 時刻への後退）は
// 型として保持するが **発火させない**（予約・本関数は決して返さない）。

/// 正常デコードできた観測の **品質シグナル**（adapter が組み立て・core が status へ写す）。
///
/// ここに「劣化の事実」を列挙するだけで、status/reason_code の決定は [`derive_mark`] が行う。
/// 劣化が一つも無ければ OK、一つでもあれば DEGRADED（決定論ルール）。
#[derive(Debug, Clone, PartialEq)]
pub struct ObservationQuality {
    /// 観測元の識別子（provenance の `source:<id>` に積む）。
    pub source_id: String,
    /// adapter のスラグ（provenance の `adapter:<slug>` に積む）。
    pub adapter_slug: String,
    /// 時刻信頼度 0.0..=1.0。`<= TIME_CONFIDENCE_DEGRADED_THRESHOLD` で DEGRADED 寄与。
    pub time_confidence: f32,
    /// 既知の劣化注釈（例: `"time-boot-ms-not-absolute"`・`"alt-ref:MSL-uncorrected"`）。
    ///
    /// 「黙って消さない」来歴。degrade 寄与かどうかは [`QualityNote::degrades`] で分類する。
    pub notes: Vec<QualityNote>,
}

/// 来歴注釈 1 件。`degrades=false` は中立な来歴（OK でも provenance に残す）、
/// `degrades=true` は信頼度/精度/完全性の低下事由（DEGRADED へ寄与し reason_code を生む）。
#[derive(Debug, Clone, PartialEq)]
pub struct QualityNote {
    /// 来歴に積む文字列（provenance にそのまま入る・no-silent-drop）。
    pub provenance: String,
    /// この注釈が「減衰して使え」を意味する劣化事由か（true=DEGRADED 寄与）。
    pub degrades: bool,
    /// DEGRADED 時に reason_code として出す短い含意（`degrades=true` のときのみ使用）。
    ///
    /// 例: `"field-missing:alt"`・`"precision-reduced:geoid-uncorrected"`。空なら省く。
    pub reason_code: String,
}

impl QualityNote {
    /// 中立な来歴（劣化ではない・OK でも残す）。
    #[must_use]
    pub fn neutral(provenance: impl Into<String>) -> Self {
        Self {
            provenance: provenance.into(),
            degrades: false,
            reason_code: String::new(),
        }
    }

    /// 劣化事由（DEGRADED へ寄与・`reason_code` は「減衰して使え」の短い含意）。
    #[must_use]
    pub fn degraded(provenance: impl Into<String>, reason_code: impl Into<String>) -> Self {
        Self {
            provenance: provenance.into(),
            degrades: true,
            reason_code: reason_code.into(),
        }
    }
}

/// `time_confidence` がこの値以下なら時刻劣化（`"time-suspect"`）として DEGRADED 寄与。
///
/// 最小実装（GNSS spoof を疑い、時刻を黙って信頼しない）。閾値の厳密な数値根拠は後続
/// increment で詰める。boot 相対時刻しか持たない上流には 0.2 程度を割り当てる運用を想定して
/// おり、その値はこの閾値で DEGRADED に落ちる。
pub const TIME_CONFIDENCE_DEGRADED_THRESHOLD: f32 = 0.5;

/// 品質シグナルから MARK を **決定論**で導出する。
///
/// ルール:
/// - 劣化事由が一つも無く `time_confidence > 閾値` → **OK**（reason_code=`"normalized"`）。
/// - `time_confidence <= 閾値` か `degrades=true` の注釈が一つでもある → **DEGRADED**。
///   reason_code は劣化事由を **`|` 連結**（時刻劣化は `"time-suspect"` を先頭）。「減衰して使え」。
///   ⚠️ `;` ではなく `|` を使う — 下流の serializer は `MARK:…;CONF:…;PROV:…` のような
///   トップレベル区切りに `;` を使うため、reason_code 自体が `;` を
///   含むと複合 DEGRADED 時にフィールド境界と衝突し分解不能になる（`musubi_types::Mark::reason_code`
///   のワイヤ文法契約を参照）。
///
/// INVALID は本関数では作らない（デコード/写像が失敗した経路は adapter が
/// [`NormalizeError`] に直接 INVALID MARK を載せる＝そもそも `ObservationQuality` を作れない）。
/// 予約 3 値（ORDER_UNKNOWN/WITHHELD/STALE_REVOCATION）も本関数では返さない。
///
/// **no-silent-drop**: 返す `Mark` は常に非空の `reason_code` を持つ（OK でも `"normalized"`）。
#[must_use]
pub fn derive_mark(quality: &ObservationQuality) -> Mark {
    // provenance は source / adapter を先頭に、全注釈を順序保存で積む（黙って消さない）。
    let mut provenance = Vec::with_capacity(quality.notes.len() + 2);
    provenance.push(format!("source:{}", quality.source_id));
    provenance.push(format!("adapter:{}", quality.adapter_slug));
    for note in &quality.notes {
        provenance.push(note.provenance.clone());
    }

    // 劣化事由（reason_code）を決定論順序で集める。時刻劣化を先頭に置く。
    let mut reasons: Vec<String> = Vec::new();
    let time_suspect = quality.time_confidence <= TIME_CONFIDENCE_DEGRADED_THRESHOLD;
    if time_suspect {
        reasons.push("time-suspect".to_string());
    }
    for note in &quality.notes {
        if note.degrades && !note.reason_code.is_empty() {
            reasons.push(note.reason_code.clone());
        }
    }

    if reasons.is_empty() {
        // 完全＝OK。基底語彙は "normalized"。
        Mark {
            status: MarkStatus::Ok,
            reason_code: "normalized".to_string(),
            provenance,
        }
    } else {
        // 一部が低下＝DEGRADED。含意は「減衰して使え」。
        // `|` 連結（`;` は下流 serializer のトップレベル区切りと衝突するため使わない）。
        Mark {
            status: MarkStatus::Degraded,
            reason_code: reasons.join("|"),
            provenance,
        }
    }
}

// =====================================================================================
// canonical Evidence node + content digest。
// =====================================================================================
//
// fan-out 手前の **単一正本（canonical node）**: normalize 直後の envelope を seal_digest で
// 1 度だけ封じ、同一 sealed envelope を下流 publisher に不変参照で渡す（clone 不要）。
//
// honest scope（over-claim 禁止）: digest は **内容 fingerprint / 署名対象**であって
// **adversary-resistant な改竄不可ではない**。署名（signature）と鍵管理は後続 increment で
// あり、ここが担保するのは非敵対的な偶発腐敗（bit-flip）の検出までである。

/// MarkStatus を canonical bytes 用の enum tag（u8）へ写す（直列化規約・固定）。
///
/// この対応を変えると digest が変わる（golden vector test が canary になる）。
const fn mark_status_tag(status: MarkStatus) -> u8 {
    match status {
        MarkStatus::Ok => 0x00,
        MarkStatus::Degraded => 0x01,
        MarkStatus::Invalid => 0x02,
        MarkStatus::OrderUnknown => 0x03,
        MarkStatus::Withheld => 0x04,
        MarkStatus::StaleRevocation => 0x05,
        // 予約・未発火（self-asserted-time 劣化）。golden は Claim.mark=Ok(0x00) なので
        // 新規 tag 0x06 を足しても hash 対象バイトは不変＝golden digest 6ee75780… 不変。
        MarkStatus::SelfAssertedTime => 0x06,
        // 予約・未発火（store-and-forward bounded buffer 飽和）。同じ理由で golden 不変。
        MarkStatus::BufferSaturated => 0x07,
    }
}

/// NaN を単一の canonical bit パターンへ畳む（finite はそのまま `to_bits()`）。
///
/// NaN の bit 表現は複数あり、`to_bits()` を素で直列化すると**意味的に同一な NaN が
/// 異なる digest を生み determinism が壊れる**（独立 verifier 2 名が指摘した latent hole）。
/// finite 値は不変＝golden digest pin に影響しない。
fn f64_canon_bits(v: f64) -> u64 {
    if v.is_nan() {
        0x7FF8_0000_0000_0000
    } else {
        v.to_bits()
    }
}

/// f32 版（[`f64_canon_bits`] と同趣旨）。
fn f32_canon_bits(v: f32) -> u32 {
    if v.is_nan() { 0x7FC0_0000 } else { v.to_bits() }
}

/// `String` を「長さ前置（u32 LE・UTF-8 バイト長）＋ UTF-8 バイト列」で push する。
fn push_str(buf: &mut Vec<u8>, s: &str) {
    // UTF-8 バイト長を u32 で前置（決定論・長さ攻撃ではなく境界明示）。
    let len = u32::try_from(s.len()).unwrap_or(u32::MAX);
    buf.extend_from_slice(&len.to_le_bytes());
    buf.extend_from_slice(s.as_bytes());
}

/// `Option<i64>` を presence byte（None=0x00 / Some=0x01++i64 LE）で push する。
fn push_opt_i64(buf: &mut Vec<u8>, v: Option<i64>) {
    match v {
        None => buf.push(0x00),
        Some(x) => {
            buf.push(0x01);
            buf.extend_from_slice(&x.to_le_bytes());
        }
    }
}

/// `Option<f64>` を presence byte（None=0x00 / Some=0x01++f64 bit-exact LE）で push する。
///
/// **bit-exact**（`f64_canon_bits`）＝Display/科学表記を挟まない（fmt_f64 の指数表記バグと
/// 同類を排除）＋ NaN は canonical パターンへ畳む。
fn push_opt_f64(buf: &mut Vec<u8>, v: Option<f64>) {
    match v {
        None => buf.push(0x00),
        Some(x) => {
            buf.push(0x01);
            buf.extend_from_slice(&f64_canon_bits(x).to_le_bytes());
        }
    }
}

/// `Option<String>` を presence byte（None=0x00 / Some=0x01++String 規則）で push する。
fn push_opt_str(buf: &mut Vec<u8>, v: Option<&str>) {
    match v {
        None => buf.push(0x00),
        Some(s) => {
            buf.push(0x01);
            push_str(buf, s);
        }
    }
}

fn push_opt_u64(buf: &mut Vec<u8>, value: Option<u64>) {
    match value {
        None => buf.push(0x00),
        Some(value) => {
            buf.push(0x01);
            buf.extend_from_slice(&value.to_le_bytes());
        }
    }
}

fn push_opt_u32(buf: &mut Vec<u8>, value: Option<u32>) {
    match value {
        None => buf.push(0x00),
        Some(value) => {
            buf.push(0x01);
            buf.extend_from_slice(&value.to_le_bytes());
        }
    }
}

fn push_opt_bool(buf: &mut Vec<u8>, value: Option<bool>) {
    match value {
        None => buf.push(0x00),
        Some(false) => buf.extend_from_slice(&[0x01, 0x00]),
        Some(true) => buf.extend_from_slice(&[0x01, 0x01]),
    }
}

/// `Position` を直列化（lat/lon は f64 bit-exact・alt は Option<f64>）。
fn push_position(buf: &mut Vec<u8>, p: &Position) {
    buf.extend_from_slice(&f64_canon_bits(p.lat_deg).to_le_bytes());
    buf.extend_from_slice(&f64_canon_bits(p.lon_deg).to_le_bytes());
    push_opt_f64(buf, p.alt_m);
}

/// `Timestamps` を直列化（observed_at: Option<i64>・received_at: i64・time_confidence: f32 bit-exact）。
fn push_timestamps(buf: &mut Vec<u8>, t: &Timestamps) {
    push_opt_i64(buf, t.observed_at);
    buf.extend_from_slice(&t.received_at.to_le_bytes());
    buf.extend_from_slice(&f32_canon_bits(t.time_confidence).to_le_bytes());
}

/// `PlatformDomain` を 1 バイトタグで直列化する。
///
/// `platform_domain` は下流の symbology 選択の写像元＝**content**（adapter が観測時に
/// 設定する実体データ）であって、
/// `signer_id`/`signed_at`/`revocation_proof` のような「署名プロセスに付随するメタデータ」
/// ではない。よって reserved-field allow-list（digest-excluded-when-unset）の対象にはせず、
/// 他の COM フィールドと同様に digest でカバーする。これを visit しないと、seal 後・
/// 署名後（signature は content_digest のみを covers）でも platform_domain を書き換えて
/// 検知を回避でき、「空の機体が地上のシンボルで描画される」取り違えを署名の外側で
/// 再現しうる。
fn push_platform_domain(buf: &mut Vec<u8>, domain: PlatformDomain) {
    let tag: u8 = match domain {
        PlatformDomain::Air => 0x00,
        PlatformDomain::Surface => 0x01,
        PlatformDomain::Ground => 0x02,
        PlatformDomain::Unknown => 0x03,
    };
    buf.push(tag);
}

fn push_host_counter(buf: &mut Vec<u8>, counter: &HostStateCounter) {
    buf.extend_from_slice(&counter.raw.to_le_bytes());
    push_opt_u64(buf, counter.delta);
}

fn push_host_counters(
    buf: &mut Vec<u8>,
    counters: &std::collections::BTreeMap<String, HostStateCounter>,
) {
    let len = u32::try_from(counters.len()).unwrap_or(u32::MAX);
    buf.extend_from_slice(&len.to_le_bytes());
    for (name, counter) in counters {
        push_str(buf, name);
        push_host_counter(buf, counter);
    }
}

fn push_host_network_interface(buf: &mut Vec<u8>, interface: &HostStateNetworkInterface) {
    push_opt_u32(buf, interface.ifindex);
    push_opt_str(buf, interface.operstate.as_deref());
    push_opt_bool(buf, interface.carrier);
    push_opt_u32(buf, interface.mtu);
    push_host_counters(buf, &interface.counters);
}

fn push_host_state_observation(buf: &mut Vec<u8>, observation: &HostStateObservation) {
    push_str(buf, &observation.source_contract_id);
    push_str(buf, &observation.host_id);
    buf.extend_from_slice(&observation.sequence.to_le_bytes());
    push_timestamps(buf, &observation.timestamps);
    push_opt_str(buf, observation.host.os.id.as_deref());
    push_opt_str(buf, observation.host.os.version_id.as_deref());
    push_opt_str(buf, observation.host.kernel_release.as_deref());
    push_opt_str(buf, observation.host.model.as_deref());
    push_opt_f64(buf, observation.host.uptime_seconds);
    push_opt_f64(buf, observation.host.load.one_min);
    push_opt_f64(buf, observation.host.load.five_min);
    push_opt_f64(buf, observation.host.load.fifteen_min);
    push_opt_u64(buf, observation.host.memory_kib.mem_total);
    push_opt_u64(buf, observation.host.memory_kib.mem_available);
    push_opt_u64(buf, observation.host.memory_kib.swap_total);
    push_opt_u64(buf, observation.host.memory_kib.swap_free);
    push_host_counters(buf, &observation.host.cpu_total_ticks);
    let len = u32::try_from(observation.network.len()).unwrap_or(u32::MAX);
    buf.extend_from_slice(&len.to_le_bytes());
    for (name, interface) in &observation.network {
        push_str(buf, name);
        push_host_network_interface(buf, interface);
    }
}

/// `ComObject` を直列化（variant tag u8 ＋ 中身）。
fn push_com_object(buf: &mut Vec<u8>, obj: &ComObject) {
    match obj {
        ComObject::PlatformState(ps) => {
            buf.push(0x00); // variant tag
            push_str(buf, &ps.platform_id);
            match &ps.position {
                None => buf.push(0x00),
                Some(pos) => {
                    buf.push(0x01);
                    push_position(buf, pos);
                }
            }
            push_opt_str(buf, ps.mode.as_deref());
            push_timestamps(buf, &ps.timestamps);
            push_platform_domain(buf, ps.platform_domain);
        }
        ComObject::HostStateObservation(observation) => {
            buf.push(0x01);
            push_host_state_observation(buf, observation);
        }
    }
}

/// `ConfidenceBasis` を variant tag(u8) ++ 中身で直列化（`push_com_object` と同型）。tag 割当を
/// 変えると golden が動く（`mark_status_tag`/`push_platform_domain` と同じ契約）。
fn push_confidence_basis(buf: &mut Vec<u8>, basis: &musubi_types::ConfidenceBasis) {
    match basis {
        musubi_types::ConfidenceBasis::AdapterAssigned => {
            buf.push(0x00); // variant tag（データ無し）
        }
        musubi_types::ConfidenceBasis::Measured { calibration_id } => {
            buf.push(0x01); // variant tag
            push_opt_str(buf, calibration_id.as_deref());
        }
    }
}

/// `Option<ConfidenceBasis>` を presence byte（None=0x00 / Some=0x01++variant）で直列化
/// （`push_opt_str` と同じ外形）。
fn push_opt_confidence_basis(buf: &mut Vec<u8>, v: Option<&musubi_types::ConfidenceBasis>) {
    match v {
        None => buf.push(0x00),
        Some(basis) => {
            buf.push(0x01);
            push_confidence_basis(buf, basis);
        }
    }
}

/// `Claim` を直列化（claim_id: String・confidence: f32 bit-exact・mark: Mark・confidence_basis）。
///
/// `confidence_basis` は **content（digest-INCLUDED・sealed）**＝観測時にアダプタが設定する記述子で
/// seal 後の改竄を検知できるよう含める（platform_domain と同じ規律。予約フィールド〔signer_id 等〕の
/// digest-excluded とは別カテゴリ）。宣言順=直列化順の慣行に従い **末尾に append** する。
fn push_claim(buf: &mut Vec<u8>, c: &Claim) {
    push_str(buf, &c.claim_id);
    buf.extend_from_slice(&f32_canon_bits(c.confidence).to_le_bytes());
    // Mark: status tag(u8) ++ reason_code(String) ++ provenance(Vec<String>)。
    buf.push(mark_status_tag(c.mark.status));
    push_str(buf, &c.mark.reason_code);
    let n = u32::try_from(c.mark.provenance.len()).unwrap_or(u32::MAX);
    buf.extend_from_slice(&n.to_le_bytes());
    for item in &c.mark.provenance {
        push_str(buf, item);
    }
    push_opt_confidence_basis(buf, c.confidence_basis.as_ref());
}

/// EvidenceEnvelope の **canonical bytes**（決定論バイト直列化）を計算する。
///
/// フィールド順 = `observation` → `claim` → `classification`。
/// **`content_digest` と `signature` は含めない**（自己参照回避・署名は digest 後段付与）。
/// 全フィールドを固定順・固定幅・little-endian・f32/f64 は bit-exact で直列化する。
///
/// `pub(crate)` ＝決定論 oracle（同入力→同 bytes）を本 crate のテストから直接当てるため。
pub(crate) fn canonical_bytes(envelope: &EvidenceEnvelope) -> Vec<u8> {
    let mut buf = Vec::new();
    push_com_object(&mut buf, &envelope.observation);
    push_claim(&mut buf, &envelope.claim);
    push_opt_str(&mut buf, envelope.classification.as_deref());
    // signature / content_digest は意図的に除外（自己参照回避）。
    // 予約フィールド（signer_id / signed_at / revocation_proof）も visit しない
    // ＝allow-list 除外（digest-excluded-when-unset・golden 6ee75780… 不変）。push_* を足すと
    // canonical_digest::digest_excludes_reserved_* が fail する（誤追加の番人・非 vacuous）。
    // trust_annotations（診断データ・同型の reserved field）も同じ理由で
    // visit しない＝digest_excludes_reserved_trust_annotations が誤追加の番人。
    buf
}

/// EvidenceEnvelope に `content_digest`（canonical bytes の SHA-256）を埋めて返す（seal）。
///
/// Normalizer が `normalize()` 直後に呼ぶ＝fan-out 手前の単一正本を確定させる。
/// `signature` には触れない（ここでは None のまま・鍵管理は本 crate の外）。
///
/// honest scope: 返る `content_digest` は **内容 fingerprint / 署名対象**であって
/// adversary-resistant な tamper-proof ではない（非敵対的な偶発腐敗の検出まで）。
#[must_use]
pub fn seal_digest(mut envelope: EvidenceEnvelope) -> EvidenceEnvelope {
    let bytes = canonical_bytes(&envelope);
    let hash: [u8; 32] = Sha256::digest(&bytes).into();
    envelope.content_digest = Some(hash);
    envelope
}

/// 受領側の **再検証**: 封じられた envelope の内容と `content_digest` の整合を確かめ MARK を返す
///
/// （tamper-evident）。canonical bytes を再計算し、保存ダイジェストと比較する:
/// - 一致 → **OK**（`reason_code="digest-verified"`）。
/// - 不一致 → **INVALID**（`reason_code="meta-tamper-detected"`・下流への含意は「使うな」）。
/// - 未シール（`content_digest=None`）→ **INVALID**（`reason_code="digest-absent"`・no-silent-drop）。
///
/// honest scope（over-claim 禁止）: これは **非敵対的な偶発腐敗（bit-flip・伝送エラー）の検出**で
/// あって、署名なしのダイジェスト単独では**敵対的改竄に耐えない**（攻撃者はダイジェストを
/// 再計算して合わせうる）。adversary 耐性は署名が付いて初めて主張できる。
/// よって reason_code・本 doc では "tamper-proof / 改竄不可" と表現しない。
#[must_use]
pub fn verify_digest(envelope: &EvidenceEnvelope) -> Mark {
    let provenance = || -> Vec<String> {
        vec![
            format!("claim:{}", envelope.claim.claim_id),
            "check:content-digest-recompute".to_string(),
        ]
    };
    match envelope.content_digest {
        None => Mark {
            // 封じられていない envelope は検証不能＝黙って通さず INVALID で surface（no-silent-drop）。
            status: MarkStatus::Invalid,
            reason_code: "digest-absent".to_string(),
            provenance: provenance(),
        },
        Some(stored) => {
            let recomputed: [u8; 32] = Sha256::digest(canonical_bytes(envelope)).into();
            if recomputed == stored {
                Mark {
                    status: MarkStatus::Ok,
                    reason_code: "digest-verified".to_string(),
                    provenance: provenance(),
                }
            } else {
                Mark {
                    // 1-bit でも内容が変われば再計算ダイジェストが一致しない＝検知（tamper-evident）。
                    status: MarkStatus::Invalid,
                    reason_code: "meta-tamper-detected".to_string(),
                    provenance: provenance(),
                }
            }
        }
    }
}

// 正規化そのものの受入 oracle は **実装側 crate に同居**する（`Normalizer` の実装は
// adapter 側にあり、core は trait/型のみで net も decoder も持たないため）。
// ここに #[ignore] の unimplemented placeholder は置かない。core 固有の決定論 property
// （round-trip 同値等）は後続 increment で本 crate にも oracle を足す。

#[cfg(test)]
mod mark_semantics {
    //! MARK status 意味論の決定論テスト（OK / DEGRADED の付与条件）。
    //! INVALID の発火は adapter 側（decode 失敗経路）でテストする。

    use super::{ObservationQuality, QualityNote, derive_mark};
    use musubi_types::MarkStatus;

    fn quality(time_confidence: f32, notes: Vec<QualityNote>) -> ObservationQuality {
        ObservationQuality {
            source_id: "uav-01".to_string(),
            adapter_slug: "mavlink/global_position_int".to_string(),
            time_confidence,
            notes,
        }
    }

    #[test]
    fn complete_observation_is_ok() {
        // 劣化注釈ゼロ・time_confidence 高 → OK・reason_code="normalized"。
        let mark = derive_mark(&quality(
            0.9,
            vec![QualityNote::neutral("alt-ref:MSL-uncorrected")],
        ));
        assert_eq!(mark.status, MarkStatus::Ok);
        assert_eq!(mark.reason_code, "normalized");
        // provenance に source / adapter / 中立注釈が順序保存で積まれる（no-silent-drop）。
        assert_eq!(mark.provenance[0], "source:uav-01");
        assert_eq!(mark.provenance[1], "adapter:mavlink/global_position_int");
        assert!(
            mark.provenance
                .iter()
                .any(|p| p == "alt-ref:MSL-uncorrected"),
            "neutral note must survive: {:?}",
            mark.provenance
        );
    }

    #[test]
    fn low_time_confidence_degrades_with_time_suspect() {
        // time_confidence <= 0.5 → DEGRADED・reason_code は "time-suspect" 先頭。
        let mark = derive_mark(&quality(
            0.2,
            vec![QualityNote::neutral("time-boot-ms-not-absolute")],
        ));
        assert_eq!(mark.status, MarkStatus::Degraded);
        assert!(
            mark.reason_code.starts_with("time-suspect"),
            "reason={}",
            mark.reason_code
        );
    }

    #[test]
    fn degrading_note_degrades_even_with_good_time() {
        // time_confidence 高でも degrades=true の注釈一つで DEGRADED。
        let mark = derive_mark(&quality(
            0.9,
            vec![QualityNote::degraded("hdg-unknown", "field-missing:hdg")],
        ));
        assert_eq!(mark.status, MarkStatus::Degraded);
        assert_eq!(mark.reason_code, "field-missing:hdg");
    }

    #[test]
    fn multiple_degradations_join_deterministically() {
        // 時刻劣化＋フィールド欠落 → "time-suspect" 先頭で "|" 連結（決定論順序）。
        // ";" ではなく "|" を使う（下流 serializer のトップレベル区切りとの衝突回避）。
        let mark = derive_mark(&quality(
            0.2,
            vec![QualityNote::degraded("alt-missing", "field-missing:alt")],
        ));
        assert_eq!(mark.status, MarkStatus::Degraded);
        assert_eq!(mark.reason_code, "time-suspect|field-missing:alt");
        assert!(
            !mark.reason_code.contains(';'),
            "reason_code must never contain ';' (downstream field separator): {}",
            mark.reason_code
        );
    }

    #[test]
    fn derive_mark_never_yields_reserved_or_empty() {
        // 境界: derive_mark は予約 3 値を返さない・reason_code は常に非空（no-silent-drop）。
        for tc in [0.0_f32, 0.5, 0.51, 1.0] {
            for notes in [
                vec![],
                vec![QualityNote::neutral("n")],
                vec![QualityNote::degraded("d", "field-missing:x")],
            ] {
                let mark = derive_mark(&quality(tc, notes));
                assert!(
                    matches!(mark.status, MarkStatus::Ok | MarkStatus::Degraded),
                    "derive_mark must only fire OK/DEGRADED, got {:?}",
                    mark.status
                );
                assert!(
                    !mark.reason_code.is_empty(),
                    "reason_code must be non-empty"
                );
            }
        }
    }
}

#[cfg(test)]
mod canonical_digest {
    //! canonical bytes 決定論 ＋ content digest の oracle。

    use super::{canonical_bytes, seal_digest};
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformDomain, PlatformState,
        Position, Timestamps,
    };

    fn sample() -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: "musubi-usv-001".to_string(),
                position: Some(Position {
                    lat_deg: 0.000_05, // ≈ 5e-5: Display なら指数表記になる値（bit-exact を強制）。
                    lon_deg: 139.767_125,
                    alt_m: Some(10.5),
                }),
                mode: None,
                timestamps: Timestamps {
                    observed_at: None,
                    received_at: 0,
                    time_confidence: 0.2,
                },
                platform_domain: musubi_types::PlatformDomain::Unknown,
            }),
            claim: Claim {
                claim_id: "c-1".to_string(),
                confidence: 0.6,
                mark: Mark {
                    status: MarkStatus::Ok,
                    reason_code: "normalized".to_string(),
                    provenance: vec!["source:x".to_string()],
                },
                // golden fixture は現実的な sealed claim を代表する＝adapter 既定値の由来（sealed content）。
                confidence_basis: Some(musubi_types::ConfidenceBasis::AdapterAssigned),
            },
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }
    }

    /// oracle 1: digest フィールド自身は canonical bytes に含めない（自己参照回避）。
    #[test]
    fn digest_excludes_self_field() {
        let a = sample(); // content_digest = None
        let mut b = sample();
        b.content_digest = Some([0u8; 32]);
        assert_eq!(
            canonical_bytes(&a),
            canonical_bytes(&b),
            "content_digest must not affect canonical bytes"
        );
    }

    /// oracle 1b: signature も canonical bytes に含めない（署名は digest 後段付与）。
    #[test]
    fn canonical_bytes_exclude_signature() {
        let a = sample();
        let mut b = sample();
        b.signature = Some(vec![1, 2, 3]);
        assert_eq!(canonical_bytes(&a), canonical_bytes(&b));
    }

    /// oracle 1c: `signer_id` を non-default にしても content_digest 不変。
    ///
    /// 予約フィールドは canonical_bytes に visit されない（push_* 呼び出しが無い）ため、設定しても
    /// golden digest を動かさない＝「digest-excluded-when-unset」の受け入れ条件を discharge。
    /// non-vacuous: maintainer が誤って push_* を足したら本 test が fail する（signature 除外 oracle と同型）。
    #[test]
    fn digest_excludes_reserved_signer_id() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut with = sample();
        with.signer_id = Some("tenant-key:alpha".to_string());
        assert_eq!(canonical_bytes(&sample()), canonical_bytes(&with));
        assert_eq!(
            seal_digest(with).content_digest.expect("sealed"),
            base,
            "signer_id must be digest-excluded (golden unchanged)"
        );
    }

    /// oracle 1d: `signed_at`（trusted-timestamp）を入れても content_digest 不変。
    #[test]
    fn digest_excludes_reserved_signed_at() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut with = sample();
        with.signed_at = Some(1_782_203_400_000);
        assert_eq!(canonical_bytes(&sample()), canonical_bytes(&with));
        assert_eq!(
            seal_digest(with).content_digest.expect("sealed"),
            base,
            "signed_at must be digest-excluded (golden unchanged)"
        );
    }

    /// oracle 1e: `revocation_proof`（staple）を入れても content_digest 不変。
    #[test]
    fn digest_excludes_reserved_revocation_proof() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut with = sample();
        with.revocation_proof = Some(vec![0xDE, 0xAD, 0xBE, 0xEF]);
        assert_eq!(canonical_bytes(&sample()), canonical_bytes(&with));
        assert_eq!(
            seal_digest(with).content_digest.expect("sealed"),
            base,
            "revocation_proof must be digest-excluded (golden unchanged)"
        );
    }

    /// oracle 1f: `trust_annotations`（out-of-band な provenance view 用の診断データ）を
    /// 入れても content_digest 不変。他の予約フィールドと同じ digest-excluded-when-unset パターン
    /// （診断の合成は読み出し側で別の型に対して行い、`derive_mark` には統合しない）。
    #[test]
    fn digest_excludes_reserved_trust_annotations() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut with = sample();
        with.trust_annotations = Some(vec![0xCA, 0xFE, 0xBA, 0xBE]);
        assert_eq!(canonical_bytes(&sample()), canonical_bytes(&with));
        assert_eq!(
            seal_digest(with).content_digest.expect("sealed"),
            base,
            "trust_annotations must be digest-excluded (golden unchanged)"
        );
    }

    /// oracle 2: 同一 envelope を 2 回 seal → digest が一致（決定論）。
    #[test]
    fn digest_deterministic() {
        let d1 = seal_digest(sample()).content_digest;
        let d2 = seal_digest(sample()).content_digest;
        assert!(d1.is_some());
        assert_eq!(d1, d2);
    }

    /// oracle 3: 内容（confidence）を変えると digest が変わる（content fingerprint）。
    #[test]
    fn digest_changes_on_field_mutation() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut mutated = sample();
        mutated.claim.confidence = 0.6001; // 1 観測値の微小変化。
        let changed = seal_digest(mutated).content_digest.expect("sealed");
        assert_ne!(base, changed, "digest must change when content changes");
    }

    /// oracle 3b: 位置を 1 bit 相当変えても digest が変わる（meta 改竄＝tamper-evident）。
    #[test]
    fn digest_changes_on_position_tamper() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut tampered = sample();
        // ComObject は S0/S1 では単一 variant＝irrefutable（variant 増加時に if let 化）。
        let ComObject::PlatformState(ps) = &mut tampered.observation else {
            panic!("expected PlatformState")
        };
        let pos = ps.position.as_mut().expect("position present");
        // lat の最下位ビットだけ反転（bit-flip 相当・偶発腐敗の検出＝tamper-evident）。
        pos.lat_deg = f64::from_bits(pos.lat_deg.to_bits() ^ 1);
        let after = seal_digest(tampered).content_digest.expect("sealed");
        assert_ne!(base, after, "1-bit meta change must change the digest");
    }

    /// oracle 3c: `platform_domain` を変えても digest が変わる
    /// （`push_com_object` への配線漏れの回帰防止）。
    ///
    /// `platform_domain` は下流 symbology の写像元＝content フィールドであって
    /// reserved-field allow-list の対象ではない。ここが digest でカバーされていないと、seal 後・
    /// 署名後（signature は content_digest のみを covers）でも書き換えを検知できず、
    /// 「空の機体が地上のシンボルで描画される」取り違えを署名の外側で再現しうる。
    #[test]
    fn digest_changes_on_platform_domain_tamper() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut tampered = sample();
        let ComObject::PlatformState(ps) = &mut tampered.observation else {
            panic!("expected PlatformState")
        };
        ps.platform_domain = PlatformDomain::Air;
        let after = seal_digest(tampered).content_digest.expect("sealed");
        assert_ne!(base, after, "platform_domain tamper must change the digest");
    }

    /// seal 後に `platform_domain` だけを改竄して
    /// 再検証すると `verify_digest` が `INVALID(meta-tamper-detected)` を返す。digest が変わる
    /// ことのみを見る `digest_changes_on_platform_domain_tamper` と対に、実際の検知経路
    /// （`verify_digest`）を通しても再現することを確かめる。
    #[test]
    fn verify_detects_platform_domain_tamper() {
        let mut sealed = seal_digest(sample());
        let ComObject::PlatformState(ps) = &mut sealed.observation else {
            panic!("expected PlatformState")
        };
        assert_eq!(
            ps.platform_domain,
            PlatformDomain::Unknown,
            "sample() baseline"
        );
        ps.platform_domain = PlatformDomain::Air;
        let mark = super::verify_digest(&sealed);
        assert_eq!(mark.status, MarkStatus::Invalid);
        assert_eq!(mark.reason_code, "meta-tamper-detected");
    }

    /// oracle 3d（二軸評価 confidence_basis の digest 包含回帰防止）:
    /// `confidence_basis` を変えても digest が変わる（`push_claim` への配線漏れ検知）。
    ///
    /// `confidence_basis` は confidence の由来（AdapterAssigned/Measured）を表す content フィールドで、
    /// アダプタ間の confidence 直接比較の可否を左右する。digest でカバーされないと、seal 後・署名後に
    /// 「adapter 既定値」を「実測値」に見せかける改竄（AdapterAssigned→Measured）を署名の外側で
    /// 再現でき、二軸評価の信頼保証が崩れる。platform_domain と同じ規律。
    #[test]
    fn digest_changes_on_confidence_basis_tamper() {
        let base = seal_digest(sample()).content_digest.expect("sealed");
        let mut tampered = sample();
        tampered.claim.confidence_basis = Some(musubi_types::ConfidenceBasis::Measured {
            calibration_id: None,
        });
        let after = seal_digest(tampered).content_digest.expect("sealed");
        assert_ne!(
            base, after,
            "confidence_basis tamper must change the digest (二軸評価 sealed coverage)"
        );
    }

    /// seal 後に `confidence_basis` だけを AdapterAssigned→Measured
    /// へ改竄して再検証すると `verify_digest` が `INVALID(meta-tamper-detected)` を返す。digest が
    /// 変わることのみを見る `digest_changes_on_confidence_basis_tamper` と対に、実際の検知経路を通す。
    #[test]
    fn verify_detects_confidence_basis_tamper() {
        let mut sealed = seal_digest(sample());
        assert_eq!(
            sealed.claim.confidence_basis,
            Some(musubi_types::ConfidenceBasis::AdapterAssigned),
            "sample() baseline"
        );
        sealed.claim.confidence_basis = Some(musubi_types::ConfidenceBasis::Measured {
            calibration_id: None,
        });
        let mark = super::verify_digest(&sealed);
        assert_eq!(mark.status, MarkStatus::Invalid);
        assert_eq!(mark.reason_code, "meta-tamper-detected");
    }

    /// oracle 4: f64 は bit-exact（Display 経由でない）。指数表記になる値で再現性を確認。
    #[test]
    fn f64_bitexact_not_display() {
        // 0.000_05 は Display だと "0.00005"、Debug だと "5e-5"。どちらの経路にも依存せず
        // to_bits() で安定直列化されることを「同値 envelope の digest 一致」で担保。
        let d1 = seal_digest(sample()).content_digest;
        let d2 = seal_digest(sample()).content_digest;
        assert_eq!(d1, d2);
    }

    /// oracle 4b: NaN は単一 canonical パターンへ畳まれる（異なる NaN bit でも digest 一致）。
    /// `to_bits()` を素で使うと意味的に同一な NaN が別 digest を生む latent hole の回帰防止。
    #[test]
    fn nan_is_canonicalized_in_digest() {
        let mut a = sample();
        let mut b = sample();
        // 2 つの異なる NaN bit パターンを同じ field（lat）に入れる。
        let ComObject::PlatformState(psa) = &mut a.observation else {
            panic!("expected PlatformState")
        };
        psa.position.as_mut().expect("pos").lat_deg = f64::from_bits(0x7FF8_0000_0000_0001);
        let ComObject::PlatformState(psb) = &mut b.observation else {
            panic!("expected PlatformState")
        };
        psb.position.as_mut().expect("pos").lat_deg = f64::from_bits(0x7FF8_0000_0000_0002);
        assert_eq!(
            seal_digest(a).content_digest,
            seal_digest(b).content_digest,
            "semantically-identical NaN must yield identical digest"
        );
    }

    /// oracle 5: golden vector — 直列化規約が変わっていないことの canary（セッション間固定）。
    /// この値が変わったら canonical bytes 規約か digest 計算が変わったということ（意図的な
    /// 変更でなければ回帰）。値は本実装で 1 度確定し pin する。
    #[test]
    fn canonical_bytes_known_vector() {
        let sealed = seal_digest(sample());
        let digest = sealed.content_digest.expect("sealed");
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(hex, super::tests_golden::SAMPLE_DIGEST_HEX, "digest={hex}");
    }

    /// seal → verify は OK（digest-verified）で round-trip する。
    #[test]
    fn verify_roundtrips_sealed_envelope() {
        let sealed = seal_digest(sample());
        let mark = super::verify_digest(&sealed);
        assert_eq!(mark.status, MarkStatus::Ok);
        assert_eq!(mark.reason_code, "digest-verified");
    }

    /// seal 後に 1-bit 改竄して再検証 → INVALID(meta-tamper-detected)。
    /// tamper-evident（偶発腐敗の検出）であって "改竄不可" ではない。
    #[test]
    fn verify_detects_one_bit_meta_tamper() {
        let mut sealed = seal_digest(sample());
        // 封じた後で観測内容を 1-bit 改竄（digest は古いまま＝不一致になる）。
        let ComObject::PlatformState(ps) = &mut sealed.observation else {
            panic!("expected PlatformState")
        };
        let pos = ps.position.as_mut().expect("position present");
        pos.lat_deg = f64::from_bits(pos.lat_deg.to_bits() ^ 1);
        let mark = super::verify_digest(&sealed);
        assert_eq!(mark.status, MarkStatus::Invalid);
        assert_eq!(mark.reason_code, "meta-tamper-detected");
    }

    /// 未シール（content_digest=None）の検証は INVALID(digest-absent)＝黙って通さない。
    #[test]
    fn verify_flags_unsealed_envelope() {
        let mark = super::verify_digest(&sample()); // content_digest = None
        assert_eq!(mark.status, MarkStatus::Invalid);
        assert_eq!(mark.reason_code, "digest-absent");
    }
}

#[cfg(test)]
mod tests_golden {
    //! golden vector の pin（canonical_bytes_known_vector が参照）。
    //! 直列化規約・digest 計算を意図せず変えたらこことの不一致で fail（canary）。
    //!
    //! **意図的な再 pin（1）**（旧 `7d3d9fe9…` → 新 `82dc9ae6…`）: `platform_domain` が
    //! `push_com_object` に配線されておらず canonical_bytes/digest/署名のいずれにも入らない
    //! ＝seal 後・署名後の platform_domain 改竄が無検知だった。`push_platform_domain` を
    //! 追加配線した意図的な破壊的変更であり、この golden 変化は回帰ではない。
    //!
    //! **意図的な再 pin（2）**（旧 `82dc9ae6…` → 新 `6ee75780…`）: 二軸評価（confidence の
    //! 精度と由来の分離）で `Claim.confidence_basis`（`ConfidenceBasis`）を
    //! **content（digest-INCLUDED・sealed）**として追加し `push_claim` に配線（`push_opt_confidence_basis`）。
    //! confidence の由来はアダプタ間比較の可否を左右する荷重フィールドで、seal 後の改竄を検知する
    //! 必要があるため予約フィールド（signer_id 等・digest-excluded）ではなく platform_domain と同じ
    //! content 扱いにした。意図的な破壊的変更であり、この golden 変化は回帰ではない。
    pub const SAMPLE_DIGEST_HEX: &str =
        "6ee7578020e523cbee00105dd0b459237af7eecdb2645e2279fd5e4b7f137227";
}

#[cfg(test)]
mod ddil_integration {
    //! 統合: DDIL プロファイル → store-and-forward → drain → ORDER_UNKNOWN MARK レジャ。
    //!
    //! 3 モジュールを跨いで配線し、ORDER_UNKNOWN が **EvidenceEnvelope を変更せず**
    //! （content_digest 不変）に side-channel で MARK レジャへ計上されること
    //! （no-silent-drop・golden digest pin を壊さない）を実証する。

    use super::ddil::{DdilProfile, ddil_behavior, order_unknown_fires};
    use super::store_and_forward::{LinkState, StoreAndForwardBuffer};
    use super::vector_clock::VectorClock;
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
    };

    fn obs(tag: &str) -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: tag.to_string(),
                position: Some(Position {
                    lat_deg: 35.0,
                    lon_deg: 139.0,
                    alt_m: None,
                }),
                mode: None,
                timestamps: Timestamps {
                    observed_at: Some(1),
                    received_at: 2,
                    time_confidence: 0.9,
                },
                platform_domain: musubi_types::PlatformDomain::Unknown,
            }),
            claim: Claim {
                claim_id: format!("c-{tag}"),
                confidence: 0.7,
                mark: Mark {
                    status: MarkStatus::Ok,
                    reason_code: "normalized".to_string(),
                    provenance: vec![format!("source:{tag}")],
                },
                confidence_basis: None,
            },
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }
    }

    /// drain 後に DDIL ポリシーで ORDER_UNKNOWN を判定し、発火分を MARK レジャへ計上する
    /// （fan-out 層が行う後処理の最小再現）。返り値 = `(dialect 相当の tag, Mark)` の MARK レジャ。
    /// **envelope 本体は触らない**（ORDER_UNKNOWN は side-channel の追記 MARK）。
    fn route_with_policy(
        buf: &mut StoreAndForwardBuffer,
        policy: super::ddil::OrderUnknownPolicy,
    ) -> Vec<(String, Mark)> {
        let mut ledger = Vec::new();
        for item in buf.drain() {
            let ComObject::PlatformState(ps) = &item.envelope().observation else {
                panic!("expected PlatformState")
            };
            let tag = ps.platform_id.clone();
            if order_unknown_fires(policy, item.order_unknown) {
                // 予約値 ORDER_UNKNOWN を直接 MARK レジャへ書く唯一の経路。
                ledger.push((
                    tag,
                    Mark {
                        status: MarkStatus::OrderUnknown,
                        reason_code: "vc-incomparable".to_string(),
                        provenance: vec!["mark-source:ddil-store-and-forward".to_string()],
                    },
                ));
            } else {
                // 因果確定 → envelope 本来の MARK をそのまま計上。
                ledger.push((tag, item.envelope().claim.mark.clone()));
            }
        }
        ledger
    }

    #[test]
    fn denied_recovery_routes_concurrent_to_order_unknown_keeps_all() {
        // golden example: DENIED 中に alpha 連鎖 3 件 ＋ beta 独立 1 件が滞留。
        // 復旧後 fan-out: beta(F1) は ORDER_UNKNOWN・alpha は因果順保持・全 4 件 multi-value 保持。
        let behavior = ddil_behavior(DdilProfile::Denied);
        assert_eq!(behavior.link_state, LinkState::Down);

        let mut buf = StoreAndForwardBuffer::new(behavior.link_state);
        buf.push(obs("E1"), VectorClock::single("alpha", 1));
        buf.push(obs("E2"), VectorClock::single("alpha", 2));
        buf.push(obs("E3"), VectorClock::single("alpha", 3));
        buf.push(obs("F1"), VectorClock::single("beta", 1));

        // DENIED の間は何も出ない（no-silent-drop で 4 件保持）。
        assert!(buf.drain().is_empty());
        assert_eq!(buf.pending_len(), 4);

        // 復旧（DTN transport が link を Up に）。
        buf.set_link(LinkState::Up);
        let ledger = route_with_policy(&mut buf, behavior.order_unknown_policy);

        // 全 4 件が MARK レジャに計上される（1 件も silent-drop しない）。
        assert_eq!(
            ledger.len(),
            4,
            "all 4 claims must be accounted (no-silent-drop)"
        );
        // beta(F1) だけが ORDER_UNKNOWN。
        let f1 = ledger
            .iter()
            .find(|(t, _)| t == "F1")
            .expect("F1 in ledger");
        assert_eq!(f1.1.status, MarkStatus::OrderUnknown);
        assert_eq!(f1.1.reason_code, "vc-incomparable");
        // alpha 連鎖は ORDER_UNKNOWN にならない（因果確定）。
        for tag in ["E1", "E2", "E3"] {
            let e = ledger
                .iter()
                .find(|(t, _)| t == tag)
                .expect("alpha in ledger");
            assert_ne!(
                e.1.status,
                MarkStatus::OrderUnknown,
                "causal {tag} must not be ORDER_UNKNOWN"
            );
        }
    }

    #[test]
    fn intermittent_marks_all_buffered_order_unknown() {
        // INTERMITTENT: Up 窓境界で因果継続性が失われる → drain 全件 ORDER_UNKNOWN（全件保持）。
        let behavior = ddil_behavior(DdilProfile::Intermittent);
        let mut buf = StoreAndForwardBuffer::new(behavior.link_state);
        buf.push(obs("E1"), VectorClock::single("alpha", 1));
        buf.push(obs("E2"), VectorClock::single("alpha", 2)); // 通常なら happens-before
        buf.set_link(LinkState::Up);
        let ledger = route_with_policy(&mut buf, behavior.order_unknown_policy);
        assert_eq!(ledger.len(), 2);
        // OnDrainAllBuffered ポリシーにより、因果確定 alpha 連鎖でも全件 ORDER_UNKNOWN。
        assert!(
            ledger
                .iter()
                .all(|(_, m)| m.status == MarkStatus::OrderUnknown),
            "INTERMITTENT must flag all buffered items ORDER_UNKNOWN"
        );
    }

    #[test]
    fn nominal_never_emits_order_unknown() {
        // NOMINAL: 即時転送・ORDER_UNKNOWN 一切なし（envelope 本来の MARK のまま計上）。
        let behavior = ddil_behavior(DdilProfile::Nominal);
        assert_eq!(behavior.link_state, LinkState::Up);
        let mut buf = StoreAndForwardBuffer::new(behavior.link_state);
        buf.push(obs("E1"), VectorClock::single("alpha", 1));
        let ledger = route_with_policy(&mut buf, behavior.order_unknown_policy);
        assert_eq!(ledger.len(), 1);
        assert_eq!(
            ledger[0].1.status,
            MarkStatus::Ok,
            "NOMINAL keeps original MARK"
        );
    }

    #[test]
    fn order_unknown_routing_does_not_mutate_envelope_digest() {
        // 単一正本: ORDER_UNKNOWN を MARK レジャへ計上しても、drain した envelope の
        // content_digest は不変（side-channel 設計＝EvidenceEnvelope を再 seal しない）。
        let sealed = super::seal_digest(obs("F1"));
        let before = sealed.content_digest;
        assert!(before.is_some());

        let mut buf = StoreAndForwardBuffer::new(LinkState::Down);
        buf.push(obs("E1"), VectorClock::single("alpha", 1)); // 基準
        buf.push(sealed, VectorClock::single("beta", 1)); // 並行 → ORDER_UNKNOWN 対象
        buf.set_link(LinkState::Up);

        let drained = buf.drain();
        // sealed envelope（beta=F1）を取り出し、digest が drain 前後で不変であることを確認。
        let f1 = drained
            .iter()
            .find(|d| {
                let ComObject::PlatformState(ps) = &d.envelope().observation else {
                    panic!("expected PlatformState")
                };
                ps.platform_id == "F1"
            })
            .expect("F1 delivered");
        assert!(f1.order_unknown, "F1 should be flagged concurrent");
        assert_eq!(
            f1.envelope().content_digest,
            before,
            "ORDER_UNKNOWN must not re-seal the envelope (single canonical digest)"
        );
        // しかも verify_digest はなお OK（内容は無改竄＝ORDER_UNKNOWN は順序の追記であって内容改竄でない）。
        assert_eq!(super::verify_digest(f1.envelope()).status, MarkStatus::Ok);
    }
}

#[cfg(test)]
mod deferred_trust {
    //! 「Unknown-as-test」を実体化する **xfail（`#[ignore]`）**。
    //!
    //! ## 何が済んでいて何が済んでいないか（honest bookkeeping）
    //!
    //! 署名の話は 2 つに分かれる。**合成アンカー機構**（synthetic issued anchor に対して
    //! COSE_Sign1 + Ed25519 で署名・検証し、1-bit forge を reject する）と、**real PKI**
    //! （実 CA が発行したアンカー・実 trust-list・live な失効状態）である。この 2 つを 1 本の
    //! xfail に conflate すると、前者が緑になった時点で後者まで緑に見える。
    //!
    //! - **合成アンカー機構**: 本 crate の外にある optional な署名 crate が担う。
    //! - **real PKI**: まだ deferred。それを表明するのが下の xfail であり、core に残る
    //!   唯一の署名関連 xfail である。
    //!
    //! ## この xfail が表明する **real-PKI** contract（今は **やらない**こと）
    //!
    //! 合成発行アンカーは real な authenticity を **証明しない**。real PKI（実 CA が発行した
    //! トラストアンカー・実 trust-list・live な失効状態）に対する検証は本 crate の評価対象外で
    //! あり、合成機構では達成されない。本 xfail はそれを **non-vacuous に** 表明する:
    //! real-PKI anchor API（実 CA chain 検証・trust-list 照合・live revocation）は core に
    //! 存在しないので、`--include-ignored` で走らせると **今は必ず fail する**。
    //!
    //! non-vacuity の根拠（real PKI が core に **本当に不在**）:
    //! - core に CA chain 検証・trust-list 照合・OCSP/CRL（live revocation）コードが無い。
    //! - 合成発行アンカーの機構は **合成**であって real CA ではない（honest scope）。
    //! - `verify_digest` は canonical bytes 再ハッシュのみで PKI を一切見ない。
    //!
    //! 本モジュールは golden に触れない: `canonical_bytes`/`seal_digest`/golden digest
    //! `6ee75780…` を変更せず、新規 production コード・新規 dep を一切足さない（テストのみ）。

    use super::seal_digest;
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
    };

    fn sample() -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: "musubi-uav-trust".to_string(),
                position: Some(Position {
                    lat_deg: 35.0,
                    lon_deg: 139.0,
                    alt_m: Some(120.0),
                }),
                mode: None,
                timestamps: Timestamps {
                    observed_at: Some(1),
                    received_at: 2,
                    time_confidence: 0.9,
                },
                platform_domain: musubi_types::PlatformDomain::Unknown,
            }),
            claim: Claim {
                claim_id: "c-trust-1".to_string(),
                confidence: 0.8,
                mark: Mark {
                    status: MarkStatus::Ok,
                    reason_code: "normalized".to_string(),
                    provenance: vec!["source:uav-trust".to_string()],
                },
                confidence_basis: None,
            },
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }
    }

    /// real-PKI 残部: Evidence が **実 CA が発行したトラストアンカー**に対して検証成功し、
    /// **live な失効状態**（OCSP/CRL）と **実 trust-list 照合**を伴うこと。
    ///
    /// これは合成アンカー機構では達成されない real-PKI contract を表明する xfail。合成機構が
    /// 達成したのは **合成発行アンカー**に対する検証であって、real CA chain・実 trust-list・
    /// live revocation ではない（honest scope: provenance ≠ live trust ≠ authorization・
    /// 合成 ≠ real PKI / authenticity）。real-PKI anchor 検証 API（CA chain・trust-list・
    /// live revocation）は core に **存在しない**。
    ///
    /// `#[ignore]` 解除（`--include-ignored`）で走らせると **今は必ず fail する**（real-PKI API が
    /// 不在で、`verify_digest` 代用も real CA を検証しないため）。
    ///
    /// ⚠️ **premature-green ガード（real PKI 用）**: この xfail を un-ignore してよいのは、**実 CA が
    /// 発行したアンカーに対する real な chain 検証 ＋ live 失効状態 ＋ 実 trust-list 照合**を実装し、
    /// 下の `verify_digest` 代用をそれに差し替えたときだけ。合成アンカーで緑化してはならない —
    /// それは別 contract（既に green）であり、real PKI の contract を満たさない（さもなくば
    /// 「合成機構」を「real PKI / authenticity」と誤読する偽の確信になる）。
    #[ignore = "deferred: a signed envelope verifies against a REAL issued PKI anchor (CA chain / trust-list / live revocation). Real PKI is implementation-deferred and outside this crate's evaluation target. The synthetic-anchor mechanism is green elsewhere and is NOT real PKI."]
    #[test]
    fn signed_envelope_verifies_against_real_pki_anchor() {
        // --- real PKI が実装されたら有効になる designed contract（今は API 不在で到達不能）---
        // 1) 実 CA が発行したアンカー鍵で seal-and-sign し、署名が充填されること。
        //    （合成アンカー機構は real CA ではない。core に real-PKI 発行は無い。）
        let signed = seal_digest(sample());
        assert!(
            signed.signature.is_some(),
            "real-PKI designed contract: a REAL CA-issued anchor key must populate \
             EvidenceEnvelope.signature (implementation-deferred — the synthetic-anchor mechanism \
             is a SYNTHETIC issued anchor, NOT real PKI; core has no real CA-issuance — this is why #[ignore])"
        );

        // 2) **実 CA chain ＋ 実 trust-list ＋ live 失効状態**に対して検証成功すること。
        //    `verify_digest` 代用は real CA を一切見ない＝real-PKI contract 未充足。
        let mark = super::verify_digest(&signed);
        assert_eq!(
            mark.status,
            MarkStatus::Ok,
            "real-PKI: a correctly signed envelope must verify against a REAL CA-issued anchor with \
             live revocation + real trust-list (the verify_digest stand-in does not check real PKI, \
             so the contract is unmet; implementation-deferred)"
        );

        // 3) real-PKI 検証 API が forge を拒否すること（real CA chain 不一致）。
        //    合成アンカーは real authenticity を証明しない。
        let mut forged = signed;
        let ComObject::PlatformState(ps) = &mut forged.observation else {
            panic!("expected PlatformState")
        };
        let pos = ps.position.as_mut().expect("position present");
        pos.lat_deg = f64::from_bits(pos.lat_deg.to_bits() ^ 1);
        assert!(
            forged.signature.is_some(),
            "real-PKI designed contract: forged-yet-signed envelope must be rejectable by REAL CA \
             anchor verification (real CA chain / live revocation) — requires real PKI (implementation-deferred)"
        );
    }
}
