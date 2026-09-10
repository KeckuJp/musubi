//! adapter **conformance harness**。
//!
//! 「適合性 conformance を通過したアダプタ数」を数えたいなら、まず conformance が実体として
//! 存在しなければならない。本 module はその最小 harness である: 任意の
//! [`musubi_core::Normalizer`] 実装（アダプタ）に golden / 逆境 fixture 群を流し、
//! 出力が COM（Common Object Model）の**構造的 invariant** を満たすかを機械検証する。
//!
//! ## 検査する invariant（ID は [`Violation::invariant`] に載る安定 token）
//!
//! | ID | 内容 | 根拠 |
//! |---|---|---|
//! | `C-DETERMINISM` | 同一入力 2 回 → bit 同一の出力（canonical_bytes/digest の決定論の観測面） |
//! | `C-OUTCOME` | fixture の期待（normalizes / rejects）と一致（誤マップ=0） |
//! | `C-SEALED` | 正規化成功時 `content_digest` が充填済（unsealed で fan-out に出ない） |
//! | `C-DIGEST` | `core::verify_digest` が OK（digest が内容と整合＝再計算一致） |
//! | `C-MARK-VOCAB` | MARK status が adapter 発火可能 3 値（OK/DEGRADED/INVALID）のみ。予約値（ORDER_UNKNOWN/WITHHELD/STALE_REVOCATION/SELF_ASSERTED_TIME/BUFFER_SATURATED）を adapter が発火しない |
//! | `C-MARK-REASON` | `reason_code` 非空（no-silent-drop）かつ `;` を含まない（下流 serializer のトップレベル区切りと衝突しない） |
//! | `C-PROVENANCE` | provenance 非空（少なくとも source 由来）かつ各 token に `;` なし |
//! | `C-COM-REQUIRED` | COM 必須フィールド充足（`platform_id` / `claim_id` 非空） | S-2（COM 6 種） |
//! | `C-POSITION-RANGE` | position があるなら WGS-84 値域（lat∈[-90,90]・lon∈[-180,180]・有限） |
//! | `C-TIME` | `received_at` は ingest 層の値をそのまま転記（`0` 決め打ちしない）・`time_confidence`∈[0,1] |
//! | `C-CONFIDENCE` | `claim.confidence`∈[0,1]・有限 | O-8 |
//! | `C-ERR-INVALID` | 拒否（`Err`）は INVALID で MARK され `source_id` を保持（黙って捨てない） |
//! | `C-NO-WRITE-SURFACE` | アダプタ production コード（`#[cfg(test)]` 以降・宣言済み test module を除く）の `pub fn`/`pub const fn` が全て呼び出し側の allow-list 内（fail-closed。allow-list に無い名前が 1 つでもあれば違反）。[`check_no_write_surface`] |
//!
//! ## 静的 invariant（write surface 走査・`run_conformance` とは別軸）
//!
//! 上表の `C-DETERMINISM`〜`C-ERR-INVALID` は [`run_conformance`] が fixture を実際に
//! normalize して調べる**ランタイム検査**（正規化出力の構造）である。これに対し
//! `C-NO-WRITE-SURFACE` は fixture を一切実行しない**ソースツリー静的走査**である。
//! fail-closed パターン（「新しい pub fn は名前に関わらずレビュー対象にする」）を、
//! リポジトリ内の CI 専用シェルスクリプトではなく、**外部のアダプタ著者が `cargo test` や単体
//! バイナリ経由で自分のクレートに対して実行できる** [`check_no_write_surface`] 関数として
//! 提供する。機能の不在の検査は、それを再実行できる人が居て初めて検査になる。
//!
//! ```no_run
//! use musubi_conformance::check_no_write_surface;
//! use std::path::Path;
//!
//! // 自分のアダプタ crate の src ディレクトリと、レビュー済みの公開関数名一覧を渡す。
//! let allowed = ["new", "normalize", "id"];
//! let violations = check_no_write_surface(Path::new("src"), &allowed);
//! assert!(violations.is_empty(), "unexpected pub fn(s) outside the reviewed allow-list");
//! ```
//!
//! ## ガードレール
//!
//! 本 module は **read のみ**（fixture bytes → normalize → 検査）。書き込み口・net・unsafe を
//! 持たない。検査対象の envelope は Evidence（観測の証拠）であって機体への指示ではない。
//!
//! ## 使い方（アダプタ著者）
//!
//! ```
//! use musubi_conformance::{ConformanceCase, run_conformance};
//! # use musubi_core::{Normalizer, NormalizeError, RawObservation};
//! # use musubi_types::EvidenceEnvelope;
//! # struct MyNormalizer;
//! # impl Normalizer for MyNormalizer {
//! #     fn normalize(&self, raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError> {
//! #         Err(NormalizeError { source_id: raw.source_id.clone(), mark: musubi_types::Mark {
//! #             status: musubi_types::MarkStatus::Invalid,
//! #             reason_code: "map-unknown".to_string(),
//! #             provenance: vec![format!("source:{}", raw.source_id)] } })
//! #     }
//! # }
//! let cases = vec![ConformanceCase::rejects("garbage", "dev-01", vec![0xFF], 1_000)];
//! let report = run_conformance("my-adapter", &MyNormalizer, &cases);
//! assert!(report.passed(), "{}", report.summary());
//! ```

use musubi_core::{NormalizeError, Normalizer, RawObservation, verify_digest};
use musubi_types::{ComObject, EvidenceEnvelope, MarkStatus};
use std::fs;
use std::path::{Path, PathBuf};

/// fixture 1 件の期待結果。
///
/// `Normalizes`（Ok＝OK/DEGRADED どちらも可・「減衰して使え」は適合）／`Rejects`
/// （Err＝INVALID で MARK された拒否）。status の細目（OK か DEGRADED か）は adapter の
/// 品質シグナル次第なので conformance では固定しない（固定すると harness が adapter の
/// 実装詳細に結合し、正当な精緻化で偽 FAIL する）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExpectedOutcome {
    /// 正規化成功（sealed な [`EvidenceEnvelope`]）を期待する。
    Normalizes,
    /// 拒否（INVALID MARK 付き [`NormalizeError`]）を期待する。
    Rejects,
}

/// conformance fixture 1 件（名前＋生観測＋期待結果）。
#[derive(Debug, Clone)]
pub struct ConformanceCase {
    /// fixture 名（violation 報告に載る・一意推奨）。
    pub name: String,
    /// アダプタに流す生観測。`received_at` は **非ゼロ**を推奨（転記契約＝
    /// 「adapter が 0 を決め打ちしていないか」を実際に検出できる値にする）。
    pub raw: RawObservation,
    /// 期待結果。
    pub expected: ExpectedOutcome,
}

impl ConformanceCase {
    /// 正規化成功を期待する fixture を作る。
    #[must_use]
    pub fn normalizes(
        name: impl Into<String>,
        source_id: impl Into<String>,
        payload: Vec<u8>,
        received_at: i64,
    ) -> Self {
        Self {
            name: name.into(),
            raw: RawObservation {
                source_id: source_id.into(),
                payload,
                received_at,
            },
            expected: ExpectedOutcome::Normalizes,
        }
    }

    /// 拒否（INVALID）を期待する fixture を作る。
    #[must_use]
    pub fn rejects(
        name: impl Into<String>,
        source_id: impl Into<String>,
        payload: Vec<u8>,
        received_at: i64,
    ) -> Self {
        Self {
            name: name.into(),
            raw: RawObservation {
                source_id: source_id.into(),
                payload,
                received_at,
            },
            expected: ExpectedOutcome::Rejects,
        }
    }
}

/// invariant 違反 1 件（どの fixture の・どの invariant が・どう破れたか）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Violation {
    /// 違反した fixture 名。
    pub case: String,
    /// 破れた invariant の安定 token（crate doc の表を参照）。
    pub invariant: &'static str,
    /// 具体的な破れ方（診断用・黙って落とさない）。
    pub detail: String,
}

/// 1 アダプタ分の conformance 実行結果。
///
/// `passed()` が true のアダプタが「conformance を通過したアダプタ」の 1 件になる
/// （配信側の条件は下流で別途成立させる＝本 harness の外）。
#[derive(Debug, Clone)]
pub struct ConformanceReport {
    /// 被検アダプタ名（報告用ラベル）。
    pub adapter: String,
    /// 実行した fixture 数。
    pub cases_run: usize,
    /// 検出した invariant 違反（空＝適合）。
    pub violations: Vec<Violation>,
}

impl ConformanceReport {
    /// 全 invariant を満たしたか（違反ゼロ）。
    #[must_use]
    pub fn passed(&self) -> bool {
        self.violations.is_empty()
    }

    /// 人間可読の結果サマリ（test の panic メッセージ用）。
    #[must_use]
    pub fn summary(&self) -> String {
        use std::fmt::Write as _;
        let mut out = format!(
            "conformance[{}]: {} case(s), {} violation(s)",
            self.adapter,
            self.cases_run,
            self.violations.len()
        );
        for v in &self.violations {
            // in-memory String への write は失敗しない（fmt::Error は無視してよい）。
            let _ = write!(out, "\n  [{}] case={} : {}", v.invariant, v.case, v.detail);
        }
        out
    }
}

/// harness 側の集計: 適合（violation ゼロ）レポート数を数える。
///
/// 「conformance を通過し、下流へ配信されたアダプタ数」という指標の
/// **conformance 通過**側をここで測定可能にする（配信側条件は本 harness のスコープ外）。
#[must_use]
pub fn adopted_count(reports: &[ConformanceReport]) -> usize {
    reports.iter().filter(|r| r.passed()).count()
}

/// アダプタ（正規化経路）が発火してよい MARK status か（3 値）。
///
/// 予約値（`OrderUnknown`/`Withheld`/`StaleRevocation`/`SelfAssertedTime`/`BufferSaturated`）
/// は core 以外の専用経路（DDIL side-channel / fan-out / 署名検証 / store-and-forward）
/// でのみ発火する契約（`musubi_types::MarkStatus` doc）。adapter の normalize 出力に現れたら違反。
#[must_use]
pub fn is_adapter_emittable(status: MarkStatus) -> bool {
    matches!(
        status,
        MarkStatus::Ok | MarkStatus::Degraded | MarkStatus::Invalid
    )
}

/// conformance 本体: fixture 群をアダプタに流し、COM 構造 invariant を検査する。
///
/// 決定論・read-only（fixture の clone 以外に副作用なし）。violation は**全件**集める
/// （最初の 1 件で止めない＝no-silent-drop の精神で全部 surface する）。
#[must_use]
pub fn run_conformance(
    adapter: &str,
    normalizer: &dyn Normalizer,
    cases: &[ConformanceCase],
) -> ConformanceReport {
    let mut violations = Vec::new();
    for case in cases {
        check_case(normalizer, case, &mut violations);
    }
    ConformanceReport {
        adapter: adapter.to_string(),
        cases_run: cases.len(),
        violations,
    }
}

/// fixture 1 件分の検査（violation を積む）。
fn check_case(normalizer: &dyn Normalizer, case: &ConformanceCase, out: &mut Vec<Violation>) {
    let push = |out: &mut Vec<Violation>, invariant: &'static str, detail: String| {
        out.push(Violation {
            case: case.name.clone(),
            invariant,
            detail,
        });
    };

    // C-DETERMINISM: 同一入力 2 回 → 同一出力（envelope 全体＝content_digest 含む bit 同一）。
    // canonical_bytes は core の pub(crate) だが、digest（SHA-256(canonical bytes)）の一致が
    // その決定論の観測面になる。
    let first = normalizer.normalize(&case.raw);
    let second = normalizer.normalize(&case.raw);
    if first != second {
        push(
            out,
            "C-DETERMINISM",
            "same input twice produced different outputs (canonical bytes/digest not deterministic)"
                .to_string(),
        );
    }

    // C-OUTCOME: 期待結果と一致するか。
    match (&first, case.expected) {
        (Ok(_), ExpectedOutcome::Rejects) => push(
            out,
            "C-OUTCOME",
            "expected rejection but adapter normalized".to_string(),
        ),
        (Err(e), ExpectedOutcome::Normalizes) => push(
            out,
            "C-OUTCOME",
            format!(
                "expected normalization but adapter rejected (reason_code={:?})",
                e.mark.reason_code
            ),
        ),
        _ => {}
    }

    // 出力そのものの構造 invariant（期待とズレていても実出力側の検査は行う＝診断材料を全部出す）。
    match &first {
        Ok(envelope) => check_envelope(envelope, case, out),
        Err(err) => check_error(err, case, out),
    }
}

/// 正規化成功（sealed envelope）側の invariant 検査。
fn check_envelope(env: &EvidenceEnvelope, case: &ConformanceCase, out: &mut Vec<Violation>) {
    let push = |out: &mut Vec<Violation>, invariant: &'static str, detail: String| {
        out.push(Violation {
            case: case.name.clone(),
            invariant,
            detail,
        });
    };

    // C-SEALED: fan-out 手前の単一正本が確定済（content_digest 充填＝seal_digest 経由）。
    if env.content_digest.is_none() {
        push(
            out,
            "C-SEALED",
            "content_digest is None (envelope not sealed before leaving the adapter)".to_string(),
        );
    } else {
        // C-DIGEST: 保存 digest が内容と整合（再計算一致＝tamper-evident の面）。
        let mark = verify_digest(env);
        if mark.status != MarkStatus::Ok {
            push(
                out,
                "C-DIGEST",
                format!(
                    "verify_digest returned {:?} (reason_code={:?}) on a freshly normalized envelope",
                    mark.status, mark.reason_code
                ),
            );
        }
    }

    // C-MARK-VOCAB: adapter は予約 MARK 値を発火しない。
    if !is_adapter_emittable(env.claim.mark.status) {
        push(
            out,
            "C-MARK-VOCAB",
            format!(
                "adapter emitted reserved MarkStatus {:?} (only Ok/Degraded/Invalid are adapter-emittable)",
                env.claim.mark.status
            ),
        );
    }

    // C-MARK-REASON / C-PROVENANCE: no-silent-drop ＋ ワイヤ文法。
    check_mark_grammar(
        &env.claim.mark.reason_code,
        &env.claim.mark.provenance,
        case,
        out,
    );

    // C-COM-REQUIRED / C-POSITION-RANGE / C-TIME: COM オブジェクト本体の構造。
    match &env.observation {
        ComObject::PlatformState(ps) => {
            if ps.platform_id.trim().is_empty() {
                push(
                    out,
                    "C-COM-REQUIRED",
                    "PlatformState.platform_id is empty".to_string(),
                );
            }
            if let Some(pos) = &ps.position {
                let lat_ok = pos.lat_deg.is_finite() && (-90.0..=90.0).contains(&pos.lat_deg);
                let lon_ok = pos.lon_deg.is_finite() && (-180.0..=180.0).contains(&pos.lon_deg);
                let alt_ok = pos.alt_m.is_none_or(f64::is_finite);
                if !(lat_ok && lon_ok && alt_ok) {
                    push(
                        out,
                        "C-POSITION-RANGE",
                        format!(
                            "position outside WGS-84 domain or non-finite: lat={} lon={} alt={:?}",
                            pos.lat_deg, pos.lon_deg, pos.alt_m
                        ),
                    );
                }
            }
            // received_at は ingest 層の値をそのまま転記（0 決め打ち・改変をしない）。
            if ps.timestamps.received_at != case.raw.received_at {
                push(
                    out,
                    "C-TIME",
                    format!(
                        "received_at not transcribed from ingest layer: raw={} envelope={}",
                        case.raw.received_at, ps.timestamps.received_at
                    ),
                );
            }
            let tc = ps.timestamps.time_confidence;
            if !(tc.is_finite() && (0.0..=1.0).contains(&tc)) {
                push(
                    out,
                    "C-TIME",
                    format!("time_confidence out of [0,1] or non-finite: {tc}"),
                );
            }
        }
        ComObject::HostStateObservation(host) => {
            if host.source_contract_id.trim().is_empty()
                || host.host_id.trim().is_empty()
                || host.sequence == 0
            {
                push(
                    out,
                    "C-COM-REQUIRED",
                    "HostStateObservation requires source_contract_id, host_id, and positive sequence"
                        .to_string(),
                );
            }
            let tc = host.timestamps.time_confidence;
            if !(tc.is_finite() && (0.0..=1.0).contains(&tc)) {
                push(
                    out,
                    "C-TIME",
                    format!("host-state time_confidence out of [0,1] or non-finite: {tc}"),
                );
            }
        }
    }

    // C-COM-REQUIRED: claim_id 非空。
    if env.claim.claim_id.trim().is_empty() {
        push(out, "C-COM-REQUIRED", "Claim.claim_id is empty".to_string());
    }

    // C-CONFIDENCE: claim.confidence の値域（観測の主張の強度・確定情報ではない）。
    let conf = env.claim.confidence;
    if !(conf.is_finite() && (0.0..=1.0).contains(&conf)) {
        push(
            out,
            "C-CONFIDENCE",
            format!("claim.confidence out of [0,1] or non-finite: {conf}"),
        );
    }
}

/// 拒否（`NormalizeError`）側の invariant 検査。
fn check_error(err: &NormalizeError, case: &ConformanceCase, out: &mut Vec<Violation>) {
    let push = |out: &mut Vec<Violation>, invariant: &'static str, detail: String| {
        out.push(Violation {
            case: case.name.clone(),
            invariant,
            detail,
        });
    };

    // C-ERR-INVALID: 拒否は INVALID で MARK される（黙って捨てる・別 status で誤魔化すの禁止）。
    if err.mark.status != MarkStatus::Invalid {
        push(
            out,
            "C-ERR-INVALID",
            format!(
                "rejection carried MarkStatus {:?} instead of Invalid",
                err.mark.status
            ),
        );
    }
    // C-ERR-INVALID: source_id を失わない（どの source の失敗かが第一級で残る）。
    if err.source_id != case.raw.source_id {
        push(
            out,
            "C-ERR-INVALID",
            format!(
                "NormalizeError.source_id={:?} does not preserve raw.source_id={:?}",
                err.source_id, case.raw.source_id
            ),
        );
    }

    check_mark_grammar(&err.mark.reason_code, &err.mark.provenance, case, out);
}

/// MARK の no-silent-drop ＋ ワイヤ文法（reason_code / provenance 共通）。
fn check_mark_grammar(
    reason_code: &str,
    provenance: &[String],
    case: &ConformanceCase,
    out: &mut Vec<Violation>,
) {
    let push = |out: &mut Vec<Violation>, invariant: &'static str, detail: String| {
        out.push(Violation {
            case: case.name.clone(),
            invariant,
            detail,
        });
    };

    if reason_code.is_empty() {
        push(
            out,
            "C-MARK-REASON",
            "reason_code is empty (no-silent-drop violated)".to_string(),
        );
    }
    // 下流 serializer は `MARK:…;CONF:…;PROV:…` のトップレベル区切りに `;` を使う。
    // reason_code / provenance token に `;` が混ざるとフィールド境界と衝突し分解不能になる。
    if reason_code.contains(';') {
        push(
            out,
            "C-MARK-REASON",
            format!("reason_code contains ';' (downstream top-level separator): {reason_code:?}"),
        );
    }
    if provenance.is_empty() {
        push(
            out,
            "C-PROVENANCE",
            "provenance is empty (must carry at least the source)".to_string(),
        );
    }
    for item in provenance {
        if item.contains(';') {
            push(
                out,
                "C-PROVENANCE",
                format!("provenance token contains ';' (wire grammar): {item:?}"),
            );
        }
    }
}

// =====================================================================================
// C-NO-WRITE-SURFACE: 静的走査（write surface / commanding surface の不在検査）
// =====================================================================================
//
// リポジトリ内 CI が使う write-surface allow-list ロジック（fail-closed パターン）を、
// 外部アダプタ開発者が自分のクレートに対して実行できる Rust 関数として移植したもの。
// 本関数自体も read-only＝ファイル読み取りのみで、書込み口も net も unsafe も持たない。

/// `dir` 配下の `.rs` ファイルを再帰的に列挙する（決定論のため sort 済み）。
fn list_rs_files(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(entries) = fs::read_dir(&d) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
                out.push(path);
            }
        }
    }
    out.sort();
    out
}

/// `line` が（前置空白を許した）`mod NAME;` または `mod NAME {` なら `NAME` を返す。
fn parse_mod_name(line: &str) -> Option<String> {
    let rest = line.trim_start().strip_prefix("mod ")?;
    let rest = rest.trim_start();
    let name: String = rest
        .chars()
        .take_while(|c| c.is_alphanumeric() || *c == '_')
        .collect();
    if name.is_empty() {
        return None;
    }
    let after = rest[name.len()..].trim_start();
    if after.starts_with(';') || after.starts_with('{') {
        Some(name)
    } else {
        None
    }
}

/// `stem`（ファイル名から拡張子を除いたもの）が `src_dir` 配下のどこかで
/// `#[cfg(test)] mod <stem>;`（または `{`）として宣言されているか。
///
/// `no_write_surface.sh` の `is_declared_cfg_test_module` と同じロジック: こう宣言された
/// ファイルは test 専用ヘルパーであり、production の write surface 走査から除外する。
fn is_declared_cfg_test_module(stem: &str, src_dir: &Path) -> bool {
    for f in list_rs_files(src_dir) {
        let Ok(content) = fs::read_to_string(&f) else {
            continue;
        };
        let lines: Vec<&str> = content.lines().collect();
        for i in 0..lines.len() {
            if lines[i].trim_end() != "#[cfg(test)]" {
                continue;
            }
            if let Some(next) = lines.get(i + 1) {
                if parse_mod_name(next).as_deref() == Some(stem) {
                    return true;
                }
            }
        }
    }
    false
}

/// `content` のうち、最初に `#[cfg(test)]` を含む行より前（production コード）だけを返す。
fn production_slice(content: &str) -> &str {
    let mut end = content.len();
    let mut offset = 0usize;
    for line in content.split_inclusive('\n') {
        if line.contains("#[cfg(test)]") {
            end = offset;
            break;
        }
        offset += line.len();
    }
    &content[..end]
}

/// `text` 中の `pub fn NAME` / `pub const fn NAME` 宣言を `(1-indexed行番号, NAME)` で全件返す。
fn scan_pub_fns(text: &str) -> Vec<(u32, String)> {
    let mut out = Vec::new();
    for (idx, line) in text.lines().enumerate() {
        for marker in ["pub const fn ", "pub fn "] {
            if let Some(pos) = line.find(marker) {
                let after = &line[pos + marker.len()..];
                let name: String = after
                    .chars()
                    .take_while(|c| c.is_alphanumeric() || *c == '_')
                    .collect();
                if !name.is_empty() {
                    out.push(((idx + 1) as u32, name));
                }
                break;
            }
        }
    }
    out
}

/// アダプタ crate の write surface（実行/コマンド発行手段）不在を静的走査で検査する。
///
/// `adapter_src_dir` 配下の production コード（`#[cfg(test)]` 以降・宣言済み test module を
/// 除く）に現れる `pub fn`/`pub const fn` を全件列挙し、`allowed_pub_fns` に無い名前が
/// 1 つでもあれば [`Violation`]（`C-NO-WRITE-SURFACE`）として報告する（fail-closed:
/// リポジトリ内 CI の write-surface allow-list と同じ設計。新規 pub fn は
/// 名前に関わらずレビュー対象になり、レビュー後に呼び出し側で `allowed_pub_fns` へ明示的に
/// 追加する）。
///
/// `run_conformance` はランタイム検査（fixture を実際に normalize する）だが、本関数は
/// fixture を一切実行しない**静的**走査＝ガードレール適合（read のみ・書込み口/net/unsafe
/// を持たない）。
#[must_use]
pub fn check_no_write_surface(adapter_src_dir: &Path, allowed_pub_fns: &[&str]) -> Vec<Violation> {
    let mut violations = Vec::new();
    if !adapter_src_dir.is_dir() {
        violations.push(Violation {
            case: adapter_src_dir.display().to_string(),
            invariant: "C-NO-WRITE-SURFACE",
            detail: "adapter_src_dir is not a directory (nothing to scan)".to_string(),
        });
        return violations;
    }

    for file in list_rs_files(adapter_src_dir) {
        let stem = file
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or_default();
        if is_declared_cfg_test_module(stem, adapter_src_dir) {
            continue;
        }
        let Ok(content) = fs::read_to_string(&file) else {
            continue;
        };
        let production = production_slice(&content);
        for (lineno, name) in scan_pub_fns(production) {
            if !allowed_pub_fns.contains(&name.as_str()) {
                violations.push(Violation {
                    case: format!("{}:{lineno}", file.display()),
                    invariant: "C-NO-WRITE-SURFACE",
                    detail: format!(
                        "pub fn `{name}` is not in the declared allow-list (write-issuing \
                         surface check; add it to allowed_pub_fns only after confirming it \
                         issues nothing back to the field device)"
                    ),
                });
            }
        }
    }
    violations
}

#[cfg(test)]
mod harness_self_test {
    //! harness が **vacuous でない**ことの検査（このリポジトリの oracle 文化）:
    //! わざと invariant を破る Normalizer を流し、違反が実際に検出されることを固定する。
    //! 「全アダプタ緑」の意味は、この self-test が赤を検出できることで初めて担保される。

    use super::{ConformanceCase, ExpectedOutcome, run_conformance};
    use musubi_core::{NormalizeError, Normalizer, RawObservation};
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
    };

    /// invariant を複数同時に破る故意の欠陥 Normalizer:
    /// - unsealed（content_digest=None）
    /// - 予約 MARK 値（`Withheld`）を adapter が発火
    /// - reason_code 空 ＋ provenance 空（silent drop）
    /// - platform_id 空・position が WGS-84 域外・received_at を 0 決め打ち（転記契約違反）
    /// - confidence が 1.0 超
    struct BrokenNormalizer;

    impl Normalizer for BrokenNormalizer {
        fn normalize(&self, _raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError> {
            Ok(EvidenceEnvelope {
                observation: ComObject::PlatformState(PlatformState {
                    platform_id: String::new(),
                    position: Some(Position {
                        lat_deg: 123.0, // > 90 = 域外
                        lon_deg: 500.0, // > 180 = 域外
                        alt_m: None,
                    }),
                    mode: None,
                    timestamps: Timestamps {
                        observed_at: None,
                        received_at: 0, // 転記契約違反（ingest は非ゼロを刻んでいる）
                        time_confidence: 0.5,
                    },
                    platform_domain: musubi_types::PlatformDomain::Unknown,
                }),
                claim: Claim {
                    claim_id: String::new(),
                    confidence: 1.5, // 域外
                    mark: Mark {
                        status: MarkStatus::Withheld, // 予約値＝adapter は発火禁止
                        reason_code: String::new(),   // silent drop
                        provenance: Vec::new(),       // silent drop
                    },
                    confidence_basis: None,
                },
                classification: None,
                signature: None,
                content_digest: None, // unsealed のまま返す
                signer_id: None,
                signed_at: None,
                revocation_proof: None,
                trust_annotations: None,
            })
        }
    }

    /// 呼び出しごとに claim_id が変わる非決定論 Normalizer（C-DETERMINISM の検出確認）。
    struct FlakyNormalizer {
        counter: std::cell::Cell<u64>,
    }

    impl Normalizer for FlakyNormalizer {
        fn normalize(&self, raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError> {
            let n = self.counter.get();
            self.counter.set(n + 1);
            Err(NormalizeError {
                source_id: raw.source_id.clone(),
                mark: Mark {
                    status: MarkStatus::Invalid,
                    reason_code: format!("attempt-{n}"), // 呼び出しごとに変わる＝非決定論
                    provenance: vec![format!("source:{}", raw.source_id)],
                },
            })
        }
    }

    fn one_case(expected: ExpectedOutcome) -> Vec<ConformanceCase> {
        vec![ConformanceCase {
            name: "self-test-input".to_string(),
            raw: RawObservation {
                source_id: "dev-01".to_string(),
                payload: vec![0x00],
                received_at: 1_726_371_737_000,
            },
            expected,
        }]
    }

    #[test]
    fn broken_normalizer_is_caught_on_every_violated_invariant() {
        let report = run_conformance(
            "broken",
            &BrokenNormalizer,
            &one_case(ExpectedOutcome::Normalizes),
        );
        assert!(!report.passed(), "harness must not pass a broken adapter");

        let violated: Vec<&str> = report.violations.iter().map(|v| v.invariant).collect();
        for expected in [
            "C-SEALED",
            "C-MARK-VOCAB",
            "C-MARK-REASON",
            "C-PROVENANCE",
            "C-COM-REQUIRED",
            "C-POSITION-RANGE",
            "C-TIME",
            "C-CONFIDENCE",
        ] {
            assert!(
                violated.contains(&expected),
                "harness must detect {expected}; got {violated:?}\n{}",
                report.summary()
            );
        }
    }

    #[test]
    fn nondeterministic_normalizer_is_caught() {
        let flaky = FlakyNormalizer {
            counter: std::cell::Cell::new(0),
        };
        let report = run_conformance("flaky", &flaky, &one_case(ExpectedOutcome::Rejects));
        assert!(
            report
                .violations
                .iter()
                .any(|v| v.invariant == "C-DETERMINISM"),
            "harness must detect nondeterminism\n{}",
            report.summary()
        );
    }

    #[test]
    fn outcome_mismatch_is_caught_both_directions() {
        // Rejects 期待に対する正常化（BrokenNormalizer は常に Ok を返す）→ C-OUTCOME。
        let report = run_conformance(
            "broken",
            &BrokenNormalizer,
            &one_case(ExpectedOutcome::Rejects),
        );
        assert!(
            report.violations.iter().any(|v| v.invariant == "C-OUTCOME"),
            "{}",
            report.summary()
        );
    }
}

#[cfg(test)]
mod write_surface_self_test {
    //! `check_no_write_surface`（静的走査）が **vacuous でない**ことの検査（harness_self_test と
    //! 同じ oracle 文化）: わざと allow-list 外の pub fn を持つダミーソースを用意し、違反が
    //! 実際に検出されることを固定する。あわせて、宣言済み `#[cfg(test)]` module や inline
    //! `#[cfg(test)] mod { .. }` 境界が正しく production 走査から除外されることも固定する。

    use super::check_no_write_surface;
    use std::fs;
    use std::path::PathBuf;

    /// テスト専用の使い捨てディレクトリを作る（プロセス/時刻で衝突を避ける）。
    fn scratch_dir(tag: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock before UNIX epoch")
            .as_nanos();
        let dir = std::env::temp_dir().join(format!(
            "musubi-conformance-write-surface-selftest-{tag}-{}-{nanos}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).expect("create scratch dir");
        dir
    }

    #[test]
    fn detects_pub_fn_outside_allow_list() {
        let dir = scratch_dir("bad");
        fs::write(
            dir.join("lib.rs"),
            "pub fn normalize() {}\npub fn fire() {}\n",
        )
        .expect("write scratch file");

        let violations = check_no_write_surface(&dir, &["normalize"]);
        assert_eq!(violations.len(), 1, "{violations:?}");
        assert_eq!(violations[0].invariant, "C-NO-WRITE-SURFACE");
        assert!(violations[0].detail.contains("fire"), "{violations:?}");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn passes_when_all_pub_fns_are_allow_listed() {
        let dir = scratch_dir("good");
        fs::write(
            dir.join("lib.rs"),
            "pub fn normalize() {}\npub const fn id() -> u8 { 0 }\n",
        )
        .expect("write scratch file");

        let violations = check_no_write_surface(&dir, &["normalize", "id"]);
        assert!(
            violations.is_empty(),
            "unexpected violations: {violations:?}"
        );

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn ignores_pub_fns_in_declared_cfg_test_modules() {
        let dir = scratch_dir("declared-test-mod");
        fs::write(dir.join("lib.rs"), "#[cfg(test)]\nmod fixtures;\n").expect("write scratch file");
        fs::write(dir.join("fixtures.rs"), "pub fn arm_everything() {}\n")
            .expect("write scratch file");

        let violations = check_no_write_surface(&dir, &[]);
        assert!(
            violations.is_empty(),
            "pub fn in a declared #[cfg(test)] mod must not count as production API: {violations:?}"
        );

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn ignores_pub_fns_after_inline_cfg_test_boundary() {
        let dir = scratch_dir("inline-test-boundary");
        fs::write(
            dir.join("lib.rs"),
            "pub fn normalize() {}\n#[cfg(test)]\nmod tests {\n    pub fn arm() {}\n}\n",
        )
        .expect("write scratch file");

        let violations = check_no_write_surface(&dir, &["normalize"]);
        assert!(
            violations.is_empty(),
            "unexpected violations: {violations:?}"
        );

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_directory_is_reported_not_silently_skipped() {
        let dir = std::env::temp_dir().join("musubi-conformance-write-surface-does-not-exist");
        let _ = fs::remove_dir_all(&dir); // ensure absence

        let violations = check_no_write_surface(&dir, &[]);
        assert_eq!(violations.len(), 1, "{violations:?}");
        assert_eq!(violations[0].invariant, "C-NO-WRITE-SURFACE");
    }
}
