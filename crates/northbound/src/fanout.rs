//! contract no-silent-drop fan-out（単一 sealed Evidence 正本 → ≥2 C2 方言）。
//!
//! the documented interface § contract（L73-76）が規範的定義（WHEN/WHERE/IF の 3 節）:
//! 1. **WHEN**: ファンアウト時、confidence / provenance / risk を各 destination 出力に保持する。
//! 2. **WHERE**: destination 能力（CoT/NVG/MIP 判定表）で運べない trust-field は
//!    "withhold / 可視ダウングレードに段階化" し MARK する。
//! 3. **IF（oracle）**: source↔sink data-diff で trust-field が "保持も MARK もされず消えたら fail"。
//!    WITHHELD も同一 MARK レジャに計上する（silent drop との区別を保証）。
//!
//! 設計（確定済み）:
//! - 単一 sealed Evidence 正本を **書き直さず**消費する（seal は S1 の `seal_digest`・ここで
//!   content_digest を再計算しない＝fan-out 後も digest 不変＝単一正本）。
//! - 各 sink ごとに [`SinkOutcome`]（Published / PublishedWithDowngrade / Withheld）を返す。
//! - 段階化規則（既定順位）:
//!   - sink が MARK status を運べる（ネイティブ or 可視ダウングレード）→ 通常 publish/可視ダウングレード。
//!   - sink が MARK を全く運べない ＋ envelope が **非 OK** → **WITHHELD**
//!     （publish しない・`MarkStatus::Withheld` を per-sink 結果と MARK レジャに計上・
//!      raw を人間判断レーンへ別ルーティング＝X-DEGRADE-2WAY）。
//!   - sink が MARK を運べない ＋ envelope が **OK** → publish（MARK 無しのリスク受容・source preview は許容）。
//!
//! honest scope（over-claim 禁止）: WITHHELD は **Musubi がその sink に出さないという publish 制御の
//! 判断**であって暗号強制ではない。proof_scope=observed（無言 trust drop=0 は攻撃予算限定の検出下界・
//! 完全排除は主張しない・the documented interface L55/X-ABSENCE）。

use crate::{DialectCapability, NorthboundPublisher, PublishError, mark_summary};
use musubi_types::{EvidenceEnvelope, Mark, MarkStatus};

/// fan-out が source↔sink diff で見る **trust-critical フィールド**（contract IF 節 + X-MARK）。
///
/// the documented interface L38（X-MARK・status enum 全値 ＋ reason_code）＋ contract L73（confidence/provenance/
/// risk）が定義する最小核。これが「保持も MARK もされず消えた」ら contract oracle は fail。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrustField {
    /// MARK の status enum（OK｜DEGRADED｜INVALID｜… ＝ "使うな/減衰して使え" の核）。
    MarkStatus,
    /// MARK の reason_code（短い含意）。
    ReasonCode,
    /// Claim の confidence（"観測の主張であって ground-truth でない" ことを伝える）。
    Confidence,
    /// MARK の provenance（来歴）。
    Provenance,
    /// contract: 二重時刻（`observed_at`／`received_at`）意味論。北向きは `observed_at`
    /// （µs→ms 変換）を優先し無ければ `received_at` にフォールバックする
    /// （[`crate::effective_time_ms`]）。現行 CoT/NVG は position を運べる sink なら常に
    /// 何らかの時刻属性を出す（capability による分岐は無い）ため、本 variant は
    /// 「時刻を一切運べない」将来の sink 向けの予約＝現行 [`capability_drop_set`] では
    /// drop 対象にならない（no-op ではなく "まだ該当 sink が無い" ことの明示）。
    Timestamps,
}

/// 単一 sink への fan-out 結果（per-sink 判断）。
///
/// `Mark` が `f32` 含みの `Claim` 系列と同じく `PartialEq` のみ（`Eq` 無し）なので本 enum も
/// `PartialEq` のみ derive する。
#[derive(Debug, Clone, PartialEq)]
pub enum SinkOutcome {
    /// MARK 専用欄を持つ sink へ通常 publish（trust-field をそのまま運んだ）。
    Published { dialect: String, bytes: Vec<u8> },
    /// 方言ネイティブ注釈欄（CoT remarks / NVG label+modifiers）へ MARK を載せて publish
    /// （可視ダウングレード＝trust-field を "運べる形に変換して保持"）。`carried` は運べた field。
    PublishedWithDowngrade {
        dialect: String,
        bytes: Vec<u8>,
        carried: Vec<TrustField>,
    },
    /// その sink へ **publish しない**（MARK を運べない sink ＋ 非 OK envelope）。
    /// `MarkStatus::Withheld` を MARK レジャに計上し、raw を人間判断レーンへ別ルーティングする。
    Withheld {
        dialect: String,
        /// WITHHELD の MARK（`status = Withheld`・reason_code は "なぜ出さなかったか"）。
        mark: Mark,
        /// この sink で "保持も MARK もされず消える" はずだった trust-field（diff の証跡）。
        would_drop: Vec<TrustField>,
    },
    /// この sink への [`NorthboundPublisher::publish`] 呼び出し自体が失敗した
    /// （regression review）。
    ///
    /// **per-publisher エラー隔離**: 旧実装は同期 for ループ内で `?` を使い、最初に失敗した
    /// publisher のエラーで関数全体が early-return し、後続 publisher への配信が丸ごとスキップ
    /// されていた（1 sink の障害が他 sink への配信を道連れにする head-of-line blocking）。
    /// 現在は 1 sink の publish 失敗を `Failed` として `outcomes` に計上し、ループを継続して
    /// 残りの publisher へ配信を試みる（no-silent-drop と同じ精神＝失敗を握り潰さず可視化しつつ、
    /// 他 sink の可用性は守る）。
    Failed {
        dialect: String,
        error: PublishError,
    },
}

impl SinkOutcome {
    /// この結果が WITHHELD か（その sink に clean publish しなかったか）。
    ///
    /// `Failed`（publish 呼び出し自体のエラー）は WITHHELD とは別カテゴリ（[`Self::is_failed`]）。
    #[must_use]
    pub fn is_withheld(&self) -> bool {
        matches!(self, SinkOutcome::Withheld { .. })
    }

    /// この結果が `Failed`（その sink への publish 呼び出しがエラーを返した）か
    /// （regression review）。
    #[must_use]
    pub fn is_failed(&self) -> bool {
        matches!(self, SinkOutcome::Failed { .. })
    }

    /// publish された出力バイト列（WITHHELD/Failed なら `None`＝出力が生成されていない）。
    #[must_use]
    pub fn bytes(&self) -> Option<&[u8]> {
        match self {
            SinkOutcome::Published { bytes, .. }
            | SinkOutcome::PublishedWithDowngrade { bytes, .. } => Some(bytes),
            SinkOutcome::Withheld { .. } | SinkOutcome::Failed { .. } => None,
        }
    }

    /// この結果の方言名。
    #[must_use]
    pub fn dialect(&self) -> &str {
        match self {
            SinkOutcome::Published { dialect, .. }
            | SinkOutcome::PublishedWithDowngrade { dialect, .. }
            | SinkOutcome::Withheld { dialect, .. }
            | SinkOutcome::Failed { dialect, .. } => dialect,
        }
    }
}

/// fan-out 全体の結果（per-sink 結果 ＋ MARK レジャ ＋ 人間判断レーン）。
///
/// contract oracle の判定材料を 1 つに束ねる: `outcomes` が各 sink の
/// Published/Downgrade/Withheld/Failed、`mark_ledger` が「WITHHELD も含む全 MARK の計上」
/// （silent drop=0 の証跡）、`human_lane` が WITHHELD された envelope の raw
/// （X-DEGRADE-2WAY＝別レーンで人間判断へ）。
///
/// `Failed`（publish 呼び出し自体のエラー・candidate A-5）は他 sink の処理をブロックしない
/// （per-publisher エラー隔離）が、`mark_ledger`/`human_lane` には計上しない（そもそも
/// publish が失敗しており MARK 対象の出力自体が存在しないため）。呼び出し側は
/// `outcomes` の `SinkOutcome::Failed` / `failed_count()` で検出する。
#[derive(Debug, Clone, PartialEq)]
pub struct FanOutReport {
    /// 各 sink の結果（入力 publisher と同順）。
    pub outcomes: Vec<SinkOutcome>,
    /// MARK レジャ（OK/DEGRADED/INVALID/WITHHELD すべてを 1 本に計上＝no-silent-drop の証跡）。
    /// `(dialect, Mark)`。WITHHELD は `status = Withheld` で載る。
    pub mark_ledger: Vec<(String, Mark)>,
    /// 人間判断レーン: WITHHELD された sink について `(dialect, raw envelope)` を別ルーティングする
    /// （X-DEGRADE-2WAY＝"WITHHELD でも raw を別レーンで人間判断へ"・情報を捨てない）。
    ///
    /// この Vec は **1 回の `fan_out` 呼び出し分の生成物**（consumer/ack/aging/上限は持たない）。
    /// パイプライン全体を通した永続的な受け皿は [`HumanLane`]（contract）を呼び出し側が持ち回り、
    /// ここから逐次 [`HumanLane::push`] する（旧実装は本 Vec を呼び出し側が集約せず黙って
    /// 捨てていた＝それ自体が silent drop だった）。
    pub human_lane: Vec<(String, EvidenceEnvelope)>,
}

impl FanOutReport {
    /// publish された（WITHHELD でも Failed でもない）出力の数。
    #[must_use]
    pub fn published_count(&self) -> usize {
        self.outcomes
            .iter()
            .filter(|o| !o.is_withheld() && !o.is_failed())
            .count()
    }

    /// WITHHELD された sink の数。
    #[must_use]
    pub fn withheld_count(&self) -> usize {
        self.outcomes.iter().filter(|o| o.is_withheld()).count()
    }

    /// publish 呼び出し自体が失敗した sink の数（regression review）。
    #[must_use]
    pub fn failed_count(&self) -> usize {
        self.outcomes.iter().filter(|o| o.is_failed()).count()
    }

    /// 指定方言の outcome を引く（テスト/呼び出し側の検査用）。
    #[must_use]
    pub fn outcome_for(&self, dialect: &str) -> Option<&SinkOutcome> {
        self.outcomes.iter().find(|o| o.dialect() == dialect)
    }
}

// =====================================================================================
// contract: 人間判断レーンの consumer/ack/滞留 aging/上限/シフト交代ハンドオフ。
// =====================================================================================
//
// 旧実装は WITHHELD envelope（`FanOutReport::human_lane`）を呼び出し側が **集約せず毎回捨てる**
// 想定になっており（application integration はこの field を一切読んでいなかった）、これ自体が
// no-silent-drop の穴だった。`HumanLane` はパイプライン全体で 1 インスタンスを持ち回す
// **bounded** な受け皿で、consumer（[`HumanLane::ack`]）・pending 一覧・滞留 aging・容量上限
// （満杯時は fail-open で最古を退避し `evicted_total` へ計上・黙って消さない）を持つ。
//
// **シフト交代ハンドオフ**（regression review）:
// features.md FEAT-A3 が明文要求していた「未 ack item を次シフトへ引き継ぐハンドオフ」が
// `HumanLaneEntry` に owner/handoff フィールドが無く未実装だった（the documented interface にも
// contract 相当の ID が不在）。[`HumanLaneEntry::owner`]（現担当者・未割当は明示的に
// `"unassigned"`）と [`HumanLane::handoff`]（前任者・後任者・引き継ぎ時刻・未 ack 残数を記録する
// **追記専用**ログ＝上書きでなく [`HumanLane::handoff_log`] へ 1 件ずつ積む）で担う。
// 担当者変更を記録するだけで、人間レーンの中身（未 ack item）は消さない。
//
// honest scope（over-claim 禁止）: **永続化はスコープ外**（in-memory のみ・プロセス再起動で
// 失われる。実運用ではディスク/DB 裏書きが必要＝後続 increment）。aging・handoff の順序は
// 壁時計でなく**論理シーケンス番号**（決定論・CI で flaky にならない・DDIL のスクリプト化
// link 状態と同趣旨）。

/// 人間判断レーンの 1 件（WITHHELD envelope ＋ 滞留管理メタ）。
#[derive(Debug, Clone, PartialEq)]
pub struct HumanLaneEntry {
    /// WITHHELD された raw envelope（情報を捨てない・X-DEGRADE-2WAY）。
    pub envelope: EvidenceEnvelope,
    /// この envelope を WITHHELD した sink の方言名。
    pub dialect: String,
    /// 挿入順（単調増加・aging の基準）。壁時計に依存しない決定論カウンタ。
    pub sequence: u64,
    /// consumer が確認済みにしたか（true＝ack 済・[`HumanLane::pending`] から除外される）。
    pub acked: bool,
    /// 現在の担当者識別子（regression review・contract）。
    ///
    /// push 時点で [`HumanLane`] の現在の担当者（未割当なら **明示的に** `"unassigned"`）を
    /// 引き継ぐ。[`HumanLane::handoff`] が呼ばれると、その時点で未 ack の全エントリの owner が
    /// 次シフトの担当者へ更新される（担当者変更を記録し人間レーンの中身は消さない）。
    pub owner: String,
}

/// シフト交代の引き継ぎレコード（追記専用ログ・regression review・
/// contract）。
///
/// [`HumanLane::handoff`] が呼ばれるたびに 1 件追記される（**上書きしない**＝人間レーンの
/// 中身を消さない）。前任者・後任者・引き継ぎ時刻（論理シーケンス）・引き継ぎ時点の未 ack 残数を
/// 記録する。
#[derive(Debug, Clone, PartialEq)]
pub struct HandoffRecord {
    /// 前任者（引き継ぎ前の担当者。まだ誰も割り当てられていなければ `"unassigned"`）。
    pub previous_owner: String,
    /// 後任者（引き継ぎ後の担当者）。
    pub next_owner: String,
    /// 引き継ぎ順（単調増加・壁時計に依存しない決定論カウンタ。[`HumanLaneEntry::sequence`]
    /// とは別軸＝アイテム挿入と handoff 発生を混同しない）。
    pub handoff_sequence: u64,
    /// 引き継ぎ時点で未 ack だった item の残数。
    pub pending_at_handoff: usize,
}

/// 人間判断レーン: WITHHELD envelope を **bounded** に保持し、consumer（ack）・滞留 aging・
/// 容量上限・**シフト交代ハンドオフ**（owner 引き継ぎ＋追記専用ログ・contract/candidate A-4）を
/// 持つ受け皿。
#[derive(Debug, Clone, PartialEq)]
pub struct HumanLane {
    capacity: usize,
    next_sequence: u64,
    entries: std::collections::VecDeque<HumanLaneEntry>,
    evicted_total: usize,
    /// 現在の担当者（新規 push はこの owner を引き継ぐ）。未割当は `"unassigned"`。
    current_owner: String,
    /// シフト交代の追記専用ログ（[`HandoffRecord`]・削除/上書きしない）。
    handoff_log: Vec<HandoffRecord>,
    next_handoff_sequence: u64,
}

impl HumanLane {
    /// 容量（1 以上に丸める）を指定して空のレーンを作る。owner 未割当は `"unassigned"`。
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity: capacity.max(1),
            next_sequence: 0,
            entries: std::collections::VecDeque::new(),
            evicted_total: 0,
            current_owner: "unassigned".to_string(),
            handoff_log: Vec::new(),
            next_handoff_sequence: 0,
        }
    }

    /// WITHHELD envelope を積む。
    ///
    /// 容量超過時は **fail-open**（プロセス死・無制限成長ではなく）: 既に `ack` 済（review 済）の
    /// 最古エントリを優先して退避し、無ければ最古の未 ack エントリを退避する。退避した entry を
    /// `Some(..)` で返す＝呼び出し側が `MarkStatus::BufferSaturated` 等で MARK/ログできる
    /// （黙って消えない）。返された退避entryの記録は呼び出し側の責務。
    pub fn push(
        &mut self,
        envelope: EvidenceEnvelope,
        dialect: impl Into<String>,
    ) -> Option<HumanLaneEntry> {
        let sequence = self.next_sequence;
        self.next_sequence += 1;
        self.entries.push_back(HumanLaneEntry {
            envelope,
            dialect: dialect.into(),
            sequence,
            acked: false,
            owner: self.current_owner.clone(),
        });
        if self.entries.len() > self.capacity {
            let evict_pos = self.entries.iter().position(|e| e.acked).unwrap_or(0);
            let evicted = self.entries.remove(evict_pos);
            self.evicted_total += 1;
            return evicted;
        }
        None
    }

    /// consumer が `sequence` のエントリを確認済みにする（`true`=見つかった）。
    pub fn ack(&mut self, sequence: u64) -> bool {
        if let Some(e) = self.entries.iter_mut().find(|e| e.sequence == sequence) {
            e.acked = true;
            true
        } else {
            false
        }
    }

    /// 未 ack の pending エントリ（consumer が消費する対象・挿入順）。
    pub fn pending(&self) -> impl Iterator<Item = &HumanLaneEntry> {
        self.entries.iter().filter(|e| !e.acked)
    }

    /// pending 件数。
    #[must_use]
    pub fn pending_len(&self) -> usize {
        self.pending().count()
    }

    /// これまでに fail-open で退避（evict）した累計件数（黙って消えた件数ではなく計上された件数）。
    #[must_use]
    pub fn evicted_total(&self) -> usize {
        self.evicted_total
    }

    /// 容量。
    #[must_use]
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// 最古 pending エントリの age（現在までの挿入ステップ数・論理時間）。pending が無ければ `None`。
    #[must_use]
    pub fn oldest_pending_age(&self) -> Option<u64> {
        self.pending().next().map(|e| {
            self.next_sequence
                .saturating_sub(e.sequence)
                .saturating_sub(1)
        })
    }

    /// 現在の担当者（新規 push はこの owner を引き継ぐ）。未割当は `"unassigned"`。
    #[must_use]
    pub fn current_owner(&self) -> &str {
        &self.current_owner
    }

    /// シフト交代ハンドオフ（regression review・contract）。
    ///
    /// 現在**未 ack** の全エントリの owner を `next_owner` へ引き継ぎ（担当者変更を記録し
    /// 人間レーンの中身は消さない＝ack 済/未 ack を問わずエントリ自体は削除しない）、
    /// [`HandoffRecord`] を追記専用ログへ 1 件積んで返す。
    ///
    /// **(IF) contract**: シフト交代後に未 ack item が owner 不在（`"unassigned"` のまま）または
    /// handoff レコード無しで残存したら fail——本メソッドはその両方を単一トランザクションで満たす
    /// （pending 全件の owner 更新 ＋ ログ追記が不可分）。
    pub fn handoff(&mut self, next_owner: impl Into<String>) -> HandoffRecord {
        let next_owner = next_owner.into();
        let previous_owner = std::mem::replace(&mut self.current_owner, next_owner.clone());
        let pending_at_handoff = self.pending_len();
        // 未 ack item を次シフトへ引き継ぐ（ack 済エントリの owner は履歴として変更しない）。
        for entry in self.entries.iter_mut().filter(|e| !e.acked) {
            entry.owner = next_owner.clone();
        }
        let handoff_sequence = self.next_handoff_sequence;
        self.next_handoff_sequence += 1;
        let record = HandoffRecord {
            previous_owner,
            next_owner,
            handoff_sequence,
            pending_at_handoff,
        };
        self.handoff_log.push(record.clone());
        record
    }

    /// シフト交代の追記専用ログ（全件・削除/上書きしない）。
    #[must_use]
    pub fn handoff_log(&self) -> &[HandoffRecord] {
        &self.handoff_log
    }
}

/// sink が **宣言した能力**（`DialectCapability`）で運べない trust-critical フィールドの集合を返す
/// （WITHHELD 判定の **routing 入力**）。
///
/// ⚠️ これは capability の**宣言**に基づく lookup であって、emit されたバイト列を解析する
/// **byte-level の source↔sink data-diff ではない**（the documented interface L75 の "data-diff" oracle は
/// 出力バイトを検査する別物＝下記 byte-level oracle が担う）。`carries_mark_status=true` を宣言
/// した publisher が実際にはバイト列から MARK を落としていても本関数は空を返す。**実際の
/// no-silent-drop の網は出力バイト検査のテスト**（`degraded_mark_is_visible_in_cot_and_nvg_output`・
/// `cot_carries_mark_in_remarks_visible_downgrade`・`degraded_mark_visible_in_label_and_modifiers`・
/// `no_trust_field_silently_lost_on_carrying_sinks`）が張る（mutation で teeth 確認済）。
///
/// 「運べない」＝ sink が MARK を一切運べない（`carries_mark_status=false`）とき、MARK 由来の
/// trust-field（status/reason_code/provenance）と confidence がすべて落ちる。結果が非空かつ MARK
/// にも載らない経路は WITHHELD で塞ぐ（[`fan_out`] が決定する）。
#[must_use]
pub fn capability_drop_set(
    envelope: &EvidenceEnvelope,
    capability: DialectCapability,
) -> Vec<TrustField> {
    let mut dropped = Vec::new();
    if capability.carries_mark_status {
        // MARK を運べる sink（ネイティブ or 注釈欄ダウングレード）は trust-field を保持できる。
        return dropped;
    }
    // MARK を全く運べない sink: MARK 由来の trust-field と confidence が source↔sink で消える。
    dropped.push(TrustField::MarkStatus);
    // reason_code は status に従属（status が運べないなら reason_code も運べない）。
    if !envelope.claim.mark.reason_code.is_empty() {
        dropped.push(TrustField::ReasonCode);
    }
    // confidence（Claim 性）も MARK 欄と同じく運べない。
    dropped.push(TrustField::Confidence);
    if !envelope.claim.mark.provenance.is_empty() {
        dropped.push(TrustField::Provenance);
    }
    dropped
}

/// envelope の Evidence status が "非 OK"（DEGRADED/INVALID/予約値）か。
///
/// WITHHELD 判定の fail-closed 側の条件（非 OK を MARK 不能 sink へ clean publish しない）。
#[must_use]
fn is_non_ok(mark: &Mark) -> bool {
    mark.status != MarkStatus::Ok
}

/// 単一 sealed Evidence 正本を複数の C2 方言 publisher へ **no-silent-drop** で fan-out する。
///
/// 段階化規則（モジュール doc の決定木）:
/// - MARK を運べる sink（`carries_mark_status=true`）:
///   - ネイティブ MARK 欄 → [`SinkOutcome::Published`]。
///   - 方言ネイティブ注釈欄（`mark_via_native_annotation=true`） → [`SinkOutcome::PublishedWithDowngrade`]。
/// - MARK を運べない sink（`carries_mark_status=false`）:
///   - envelope が **非 OK** → [`SinkOutcome::Withheld`]（publish しない・WITHHELD を MARK レジャに
///     計上・raw を `human_lane` へ）。
///   - envelope が **OK** → [`SinkOutcome::Published`]（MARK 無しのリスク受容・source preview 許容）。
///
/// **単一正本**: 各 publisher は同一 `envelope`（不変参照）を消費し、ここで `content_digest` を
/// 再計算しない＝fan-out 後も digest 不変（regression property ①の oracle）。
///
/// **per-publisher エラー隔離**（regression review）: いずれかの sink の
/// [`NorthboundPublisher::publish`] が失敗しても、その sink の結果を [`SinkOutcome::Failed`] として
/// 計上するだけで、ループを継続して**残りの publisher への配信を試みる**。旧実装は同期 for ループ
/// 内で `?` を使い最初の publisher エラーで関数全体が early-return していた（1 sink の障害が他の
/// 正常な C2 sink への配信まで丸ごとスキップさせる head-of-line blocking＝North Star の複数 C2
/// 再利用という中核経路の欠陥）。呼び出し側は `report.failed_count()` / `outcomes` の
/// `SinkOutcome::Failed` で失敗を検出できる（黙って握り潰さない＝no-silent-drop と同じ精神）。
pub fn fan_out(
    envelope: &EvidenceEnvelope,
    publishers: &[&dyn NorthboundPublisher],
) -> FanOutReport {
    let mut outcomes = Vec::with_capacity(publishers.len());
    let mut mark_ledger = Vec::new();
    let mut human_lane = Vec::new();

    let mark = &envelope.claim.mark;

    for publisher in publishers {
        let dialect = publisher.dialect().to_string();
        let cap = publisher.capability();
        let dropped = capability_drop_set(envelope, cap);

        if cap.carries_mark_status {
            // MARK を運べる sink: 通常 publish。可視ダウングレードか否かで outcome を分ける。
            // この sink の publish が失敗しても他 sink への配信は続ける（per-publisher 隔離）。
            let bytes = match publisher.publish(envelope) {
                Ok(bytes) => bytes,
                Err(error) => {
                    outcomes.push(SinkOutcome::Failed { dialect, error });
                    continue;
                }
            };
            // MARK レジャには envelope 由来の MARK を計上（status/reason_code/provenance）。
            mark_ledger.push((dialect.clone(), mark.clone()));
            if cap.mark_via_native_annotation {
                outcomes.push(SinkOutcome::PublishedWithDowngrade {
                    dialect,
                    bytes,
                    carried: vec![
                        TrustField::MarkStatus,
                        TrustField::ReasonCode,
                        TrustField::Confidence,
                        TrustField::Provenance,
                    ],
                });
            } else {
                outcomes.push(SinkOutcome::Published { dialect, bytes });
            }
        } else if is_non_ok(mark) {
            // MARK を運べない sink ＋ 非 OK envelope → WITHHELD（clean publish しない）。
            let withheld_mark = Mark {
                status: MarkStatus::Withheld,
                reason_code: format!("sink-cannot-carry-mark:{dialect}"),
                provenance: vec![
                    format!("withheld-from:{dialect}"),
                    format!("source-status:{}", mark_summary(mark)),
                ],
            };
            mark_ledger.push((dialect.clone(), withheld_mark.clone()));
            // X-DEGRADE-2WAY: raw を人間判断レーンへ別ルーティング（情報を捨てない）。
            human_lane.push((dialect.clone(), envelope.clone()));
            outcomes.push(SinkOutcome::Withheld {
                dialect,
                mark: withheld_mark,
                would_drop: dropped,
            });
        } else {
            // MARK を運べない sink ＋ OK envelope → MARK 無しで publish（リスク受容・source preview 許容）。
            // この sink の publish が失敗しても他 sink への配信は続ける（per-publisher 隔離）。
            match publisher.publish(envelope) {
                Ok(bytes) => {
                    mark_ledger.push((dialect.clone(), mark.clone()));
                    outcomes.push(SinkOutcome::Published { dialect, bytes });
                }
                Err(error) => {
                    outcomes.push(SinkOutcome::Failed { dialect, error });
                }
            }
        }
    }

    FanOutReport {
        outcomes,
        mark_ledger,
        human_lane,
    }
}

#[cfg(test)]
mod tests {
    //! contract no-silent-drop の oracle（fan-out・source↔sink diff・WITHHELD）。

    use super::{TrustField, capability_drop_set, fan_out};
    use crate::{CotPublisher, DialectCapability, NorthboundPublisher, NvgPublisher, PublishError};
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
    };

    /// MARK を一切運べない仮想 sink（位置のみの stripped C2・remarks/label 欄なし）。
    /// fan-out で WITHHELD 判定の対象（capability.carries_mark_status=false）。
    #[derive(Debug, Clone, Default)]
    struct StrippedSink;

    impl NorthboundPublisher for StrippedSink {
        fn dialect(&self) -> &str {
            "stripped-cot"
        }
        fn capability(&self) -> DialectCapability {
            DialectCapability::position_only()
        }
        fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
            // 位置のみの最小 CoT（MARK/remarks を一切載せない＝clean publish）。
            let ComObject::PlatformState(ps) = &envelope.observation else {
                panic!("expected PlatformState")
            };
            let pos = ps.position.as_ref().ok_or(PublishError {
                dialect: "stripped-cot".to_string(),
                reason: "missing position".to_string(),
            })?;
            Ok(format!(
                "<event uid=\"{}\"><point lat=\"{}\" lon=\"{}\"/></event>",
                ps.platform_id, pos.lat_deg, pos.lon_deg
            )
            .into_bytes())
        }
    }

    fn envelope(status: MarkStatus, reason: &str) -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: "uav-01".to_string(),
                position: Some(Position {
                    lat_deg: 35.681_236,
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
                    status,
                    reason_code: reason.to_string(),
                    provenance: vec!["source:uav-01".to_string()],
                },
                confidence_basis: None,
            },
            classification: None,
            signature: None,
            // content_digest は本テストの単一正本不変性チェックで seal する（ここでは未シール）。
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }
    }

    // === regression property ① 単一 Evidence → ≥2 C2 方言 fan-out（North Star 2x2 の C2 軸） ===

    #[test]
    fn single_evidence_fans_out_to_cot_and_nvg() {
        let env = envelope(MarkStatus::Ok, "normalized");
        let cot = CotPublisher::new();
        let nvg = NvgPublisher::new();
        let report = fan_out(&env, &[&cot, &nvg]);

        // 2 C2 方言ともに出力が生成される（North Star の C2 軸＝CoT + NVG）。
        assert_eq!(report.published_count(), 2);
        assert_eq!(report.withheld_count(), 0);

        let cot_out = report.outcome_for("cot").expect("cot outcome");
        let nvg_out = report.outcome_for("nvg").expect("nvg outcome");
        let cot_xml = String::from_utf8(cot_out.bytes().expect("cot bytes").to_vec()).unwrap();
        let nvg_xml = String::from_utf8(nvg_out.bytes().expect("nvg bytes").to_vec()).unwrap();
        assert!(cot_xml.contains("<event "), "{cot_xml}");
        assert!(nvg_xml.contains("<nvg "), "{nvg_xml}");
        // 両方とも同一 platform_id（単一正本から派生）。
        assert!(cot_xml.contains("uid=\"uav-01\""), "{cot_xml}");
        assert!(nvg_xml.contains("uri=\"uav-01\""), "{nvg_xml}");
    }

    #[test]
    fn fan_out_preserves_single_canonical_digest() {
        // regression property ① oracle: fan-out は単一正本（sealed envelope）を書き直さない
        // ＝ content_digest が fan-out 前後で不変（各 publisher は不変参照を消費）。
        // 注: これは **設計契約（design-contract）の固定**であって振る舞いの oracle ではない＝
        // `fan_out(&EvidenceEnvelope, ...)` が不変借用なので実装に依らず digest は変えられない
        // （borrow-checker が保証）。digest の振る舞い検証は core の contract oracle が担う。
        let sealed = musubi_core::seal_digest(envelope(MarkStatus::Ok, "normalized"));
        let before = sealed.content_digest;
        let cot = CotPublisher::new();
        let nvg = NvgPublisher::new();
        let _ = fan_out(&sealed, &[&cot, &nvg]);
        assert_eq!(
            sealed.content_digest, before,
            "fan-out must not mutate the single canonical digest"
        );
    }

    // === regression property ② source↔sink diff が dropped trust-field を報告 ===

    #[test]
    fn diff_reports_dropped_fields_for_stripped_sink() {
        let env = envelope(MarkStatus::Degraded, "time-suspect");
        // MARK を運べる sink（CoT/NVG）→ 落ちない。
        assert!(capability_drop_set(&env, CotPublisher::new().capability()).is_empty());
        assert!(capability_drop_set(&env, NvgPublisher::new().capability()).is_empty());
        // MARK を運べない sink → status/reason_code/confidence/provenance が落ちる。
        let dropped = capability_drop_set(&env, DialectCapability::position_only());
        assert!(dropped.contains(&TrustField::MarkStatus), "{dropped:?}");
        assert!(dropped.contains(&TrustField::ReasonCode), "{dropped:?}");
        assert!(dropped.contains(&TrustField::Confidence), "{dropped:?}");
    }

    #[test]
    fn no_trust_field_silently_lost_on_carrying_sinks() {
        // contract IF 節: source↔sink で trust-field が "保持も MARK もされず消える" =0。
        // **実出力バイト列を検査する byte-level oracle**（declared `carried` Vec でなく＝serializer が
        // 黙って MARK を落としたら捕捉できる。capability_drop_set の宣言を信用しない）。
        let env = envelope(MarkStatus::Degraded, "time-suspect");
        let cot = CotPublisher::new();
        let nvg = NvgPublisher::new();
        let report = fan_out(&env, &[&cot, &nvg]);
        for o in &report.outcomes {
            // 運べる sink は WITHHELD でなく publish される。
            assert!(
                !o.is_withheld(),
                "carrying sink must publish, not withhold: {}",
                o.dialect()
            );
            // 実バイト列に MARK の status と reason_code が現れる（黙って消えていない）。
            let xml = String::from_utf8(o.bytes().expect("bytes").to_vec()).expect("utf8");
            assert!(
                xml.contains("DEGRADED"),
                "trust status silently lost in {}: {xml}",
                o.dialect()
            );
            assert!(
                xml.contains("time-suspect"),
                "reason_code silently lost in {}: {xml}",
                o.dialect()
            );
        }
    }

    // === regression property ③ 非 OK envelope を MARK 不能 sink へ → WITHHELD（clean publish しない） ===

    #[test]
    fn non_ok_envelope_to_stripped_sink_is_withheld() {
        // golden シナリオ: DEGRADED(time-suspect) envelope を stripped sink（MARK 不能）へ。
        let env = envelope(MarkStatus::Degraded, "time-suspect");
        let stripped = StrippedSink;
        let report = fan_out(&env, &[&stripped]);

        // (a) clean publish が生成されない（その sink に出力が無い）。
        let out = report.outcome_for("stripped-cot").expect("outcome");
        assert!(out.is_withheld(), "non-OK to MARK-less sink must withhold");
        assert!(out.bytes().is_none(), "no clean output must be produced");

        // (b) MARK レジャに WITHHELD が 1 件計上される（silent drop ではない）。
        assert_eq!(report.withheld_count(), 1);
        let withheld = report
            .mark_ledger
            .iter()
            .find(|(d, _)| d == "stripped-cot")
            .expect("ledger entry");
        assert_eq!(withheld.1.status, MarkStatus::Withheld);
        assert!(
            withheld.1.reason_code.starts_with("sink-cannot-carry-mark"),
            "reason={}",
            withheld.1.reason_code
        );

        // (c) X-DEGRADE-2WAY: raw が人間判断レーンへ別ルーティングされる（情報を捨てない）。
        assert_eq!(report.human_lane.len(), 1);
        assert_eq!(report.human_lane[0].0, "stripped-cot");
        assert_eq!(
            report.human_lane[0].1.claim.mark.status,
            MarkStatus::Degraded
        );
    }

    #[test]
    fn ok_envelope_to_stripped_sink_publishes_without_mark() {
        // 段階化規則の最後の枝: OK envelope は MARK 不能 sink へ MARK 無しで publish（許容）。
        // WITHHELD は "非 OK を clean 位置に隠す" を防ぐためで、OK は隠す劣化が無い。
        let env = envelope(MarkStatus::Ok, "normalized");
        let stripped = StrippedSink;
        let report = fan_out(&env, &[&stripped]);
        let out = report.outcome_for("stripped-cot").expect("outcome");
        assert!(!out.is_withheld(), "OK envelope need not be withheld");
        assert!(out.bytes().is_some());
        assert_eq!(report.withheld_count(), 0);
        assert!(report.human_lane.is_empty());
    }

    #[test]
    fn mixed_fanout_withholds_only_the_markless_sink() {
        // 混在 fan-out: 非 OK envelope を [CoT(運べる), NVG(運べる), stripped(運べない)] へ。
        // CoT/NVG は可視ダウングレードで publish・stripped だけ WITHHELD。
        let env = envelope(MarkStatus::Invalid, "meta-tamper-detected");
        let cot = CotPublisher::new();
        let nvg = NvgPublisher::new();
        let stripped = StrippedSink;
        let report = fan_out(&env, &[&cot, &nvg, &stripped]);

        assert_eq!(report.published_count(), 2, "cot+nvg publish");
        assert_eq!(report.withheld_count(), 1, "stripped withholds");
        assert!(!report.outcome_for("cot").unwrap().is_withheld());
        assert!(!report.outcome_for("nvg").unwrap().is_withheld());
        assert!(report.outcome_for("stripped-cot").unwrap().is_withheld());

        // MARK レジャは 3 件すべてを計上（WITHHELD 含む＝no-silent-drop の証跡）。
        assert_eq!(report.mark_ledger.len(), 3);
    }

    // === A-5: per-publisher エラー隔離（regression review） ===

    /// 常に publish に失敗する仮想 sink（他 sink への配信継続を検証するための故障注入）。
    #[derive(Debug, Clone, Default)]
    struct AlwaysFailingSink;

    impl NorthboundPublisher for AlwaysFailingSink {
        fn dialect(&self) -> &str {
            "always-failing"
        }
        fn capability(&self) -> DialectCapability {
            // MARK を運べる sink 側の分岐（publisher.publish の呼び出し箇所①）を経由させる。
            DialectCapability {
                carries_position: true,
                carries_mark_status: true,
                mark_via_native_annotation: true,
            }
        }
        fn publish(&self, _envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
            Err(PublishError {
                dialect: "always-failing".to_string(),
                reason: "synthetic publish failure (A-5 isolation test)".to_string(),
            })
        }
    }

    #[test]
    fn one_publisher_failure_does_not_block_delivery_to_other_publishers() {
        // N=3 配信先（先頭 1 つが必ず失敗）。旧実装は同期 for ループの `?` で最初の publisher
        // エラーにより関数全体が early-return し、後続 2 つへの配信が丸ごとスキップされていた
        // （head-of-line blocking）。修正後は残り N-1 の正常な sink へ配信が継続すること。
        let env = envelope(MarkStatus::Ok, "normalized");
        let failing = AlwaysFailingSink;
        let cot = CotPublisher::new();
        let nvg = NvgPublisher::new();
        let report = fan_out(&env, &[&failing, &cot, &nvg]);

        // 失敗した sink は Failed として計上される（黙って消えない）。
        assert_eq!(report.failed_count(), 1, "exactly one sink failed");
        let failed = report
            .outcome_for("always-failing")
            .expect("failed outcome present");
        assert!(failed.is_failed(), "must be reported as Failed");
        assert!(
            !failed.is_withheld(),
            "Failed is not the same category as Withheld"
        );
        assert!(failed.bytes().is_none());

        // 残る N-1（cot・nvg）は失敗した publisher に道連れにされず配信された。
        assert_eq!(
            report.published_count(),
            2,
            "the other N-1 publishers must still be delivered to"
        );
        assert!(!report.outcome_for("cot").unwrap().is_withheld());
        assert!(!report.outcome_for("cot").unwrap().is_failed());
        assert!(report.outcome_for("cot").unwrap().bytes().is_some());
        assert!(!report.outcome_for("nvg").unwrap().is_withheld());
        assert!(!report.outcome_for("nvg").unwrap().is_failed());
        assert!(report.outcome_for("nvg").unwrap().bytes().is_some());

        // outcomes は入力 publisher と同順を保つ（failing が先頭）。
        assert_eq!(report.outcomes.len(), 3);
        assert!(report.outcomes[0].is_failed());
    }

    #[test]
    fn failing_publisher_in_the_middle_still_lets_later_publishers_through() {
        // 失敗する sink が先頭でなく中間にある場合でも、その後段（末尾）の sink へ配信が届くこと
        // （「最初のエラーで止まる」旧実装の再発防止＝位置に依らず隔離される）。
        let env = envelope(MarkStatus::Ok, "normalized");
        let cot = CotPublisher::new();
        let failing = AlwaysFailingSink;
        let nvg = NvgPublisher::new();
        let report = fan_out(&env, &[&cot, &failing, &nvg]);

        assert_eq!(report.failed_count(), 1);
        assert_eq!(
            report.published_count(),
            2,
            "cot (before) and nvg (after) both delivered"
        );
        assert!(report.outcome_for("cot").unwrap().bytes().is_some());
        assert!(report.outcome_for("nvg").unwrap().bytes().is_some());
    }

    // === DEGRADED が CoT/NVG 出力で可視化される（隠さない・grep で MARK が出る） ===

    #[test]
    fn degraded_mark_is_visible_in_cot_and_nvg_output() {
        // test ②: DEGRADED envelope を CoT/NVG に出すと、両出力が MARK の劣化を可視化する。
        let env = envelope(MarkStatus::Degraded, "time-suspect");
        let cot = CotPublisher::new();
        let nvg = NvgPublisher::new();
        let report = fan_out(&env, &[&cot, &nvg]);

        let cot_xml =
            String::from_utf8(report.outcome_for("cot").unwrap().bytes().unwrap().to_vec())
                .unwrap();
        let nvg_xml =
            String::from_utf8(report.outcome_for("nvg").unwrap().bytes().unwrap().to_vec())
                .unwrap();

        // CoT: <remarks> に DEGRADED:time-suspect が出る（TAK が表示＝可視ダウングレード）。
        assert!(
            cot_xml.contains("DEGRADED"),
            "CoT must surface MARK: {cot_xml}"
        );
        assert!(cot_xml.contains("time-suspect"), "{cot_xml}");
        // NVG: label / modifiers に DEGRADED が出る。
        assert!(
            nvg_xml.contains("DEGRADED"),
            "NVG must surface MARK: {nvg_xml}"
        );
        assert!(nvg_xml.contains("time-suspect"), "{nvg_xml}");
    }
}

#[cfg(test)]
mod human_lane_tests {
    //! contract: 人間判断レーンの consumer（ack）・pending・滞留 aging・容量上限（fail-open eviction）。

    use super::HumanLane;
    use musubi_types::{Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState};

    fn env(tag: &str) -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: tag.to_string(),
                position: None,
                mode: None,
                timestamps: musubi_types::Timestamps {
                    observed_at: None,
                    received_at: 0,
                    time_confidence: 0.2,
                },
                platform_domain: musubi_types::PlatformDomain::Unknown,
            }),
            claim: Claim {
                claim_id: format!("c-{tag}"),
                confidence: 0.6,
                mark: Mark {
                    status: MarkStatus::Degraded,
                    reason_code: "time-suspect".to_string(),
                    provenance: vec![],
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

    #[test]
    fn push_within_capacity_never_evicts() {
        let mut lane = HumanLane::new(4);
        for tag in ["A", "B", "C"] {
            assert!(lane.push(env(tag), "stripped-cot").is_none());
        }
        assert_eq!(lane.pending_len(), 3);
        assert_eq!(lane.evicted_total(), 0);
    }

    #[test]
    fn push_over_capacity_fail_opens_by_evicting_oldest_unacked() {
        // 容量 2・全件未 ack で 3 件目を push → 最古（A）を fail-open で退避（捨てるが計上する）。
        let mut lane = HumanLane::new(2);
        assert!(lane.push(env("A"), "stripped-cot").is_none());
        assert!(lane.push(env("B"), "stripped-cot").is_none());
        let evicted = lane
            .push(env("C"), "stripped-cot")
            .expect("capacity exceeded must evict, not silently grow unbounded");
        assert_eq!(evicted.sequence, 0, "A (oldest, seq=0) must be evicted");
        assert_eq!(lane.pending_len(), 2, "B and C remain");
        assert_eq!(lane.evicted_total(), 1);
    }

    #[test]
    fn ack_marks_entry_and_removes_it_from_pending() {
        let mut lane = HumanLane::new(4);
        lane.push(env("A"), "stripped-cot");
        lane.push(env("B"), "stripped-cot");
        assert_eq!(lane.pending_len(), 2);
        assert!(lane.ack(0), "sequence 0 (A) must be found and acked");
        assert_eq!(lane.pending_len(), 1, "acked entry leaves pending");
        assert!(!lane.ack(999), "unknown sequence must not be found");
    }

    #[test]
    fn eviction_prefers_acked_entries_over_unacked_ones() {
        // 容量 2: A を ack 済にしてから C を push → 未 ack の B ではなく ack 済の A が退避される
        // （review 済データを優先して手放す方が、未レビューを失うより望ましい）。
        let mut lane = HumanLane::new(2);
        lane.push(env("A"), "stripped-cot");
        lane.push(env("B"), "stripped-cot");
        assert!(lane.ack(0)); // A を ack 済に。
        let evicted = lane
            .push(env("C"), "stripped-cot")
            .expect("over capacity must evict");
        assert_eq!(
            evicted.sequence, 0,
            "acked A must be evicted before unacked B"
        );
        // 残るのは未 ack の B と新規 C（どちらも pending）。
        assert_eq!(lane.pending_len(), 2);
    }

    #[test]
    fn oldest_pending_age_grows_as_new_entries_arrive() {
        let mut lane = HumanLane::new(10);
        lane.push(env("A"), "stripped-cot");
        assert_eq!(lane.oldest_pending_age(), Some(0), "just inserted → age 0");
        lane.push(env("B"), "stripped-cot");
        lane.push(env("C"), "stripped-cot");
        assert_eq!(
            lane.oldest_pending_age(),
            Some(2),
            "A is now 2 insertions behind the newest"
        );
        // A を ack すると最古 pending は B に移り age が縮む。
        lane.ack(0);
        assert_eq!(lane.oldest_pending_age(), Some(1));
    }

    #[test]
    fn empty_lane_has_no_oldest_age() {
        let lane = HumanLane::new(4);
        assert_eq!(lane.oldest_pending_age(), None);
        assert_eq!(lane.pending_len(), 0);
    }

    #[test]
    fn end_to_end_withheld_envelope_flows_into_human_lane() {
        // 実際の fan_out の WITHHELD 出力を HumanLane に流し込む配線（application integration がやること
        // の最小再現）。
        use super::fan_out;
        use crate::{DialectCapability, NorthboundPublisher, PublishError};

        struct StrippedSink;
        impl NorthboundPublisher for StrippedSink {
            fn dialect(&self) -> &str {
                "stripped-cot"
            }
            fn capability(&self) -> DialectCapability {
                DialectCapability::position_only()
            }
            fn publish(&self, _envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
                Ok(b"<event/>".to_vec())
            }
        }

        let e = env("uav-01");
        let stripped = StrippedSink;
        let report = fan_out(&e, &[&stripped]);
        assert_eq!(report.human_lane.len(), 1);

        let mut lane = HumanLane::new(8);
        let mut evicted_total = 0usize;
        for (dialect, envelope) in report.human_lane {
            if lane.push(envelope, dialect).is_some() {
                evicted_total += 1;
            }
        }
        assert_eq!(lane.pending_len(), 1, "withheld envelope reaches the lane");
        assert_eq!(evicted_total, 0);
    }

    // === A-4: シフト交代ハンドオフ（regression review・contract） ===

    #[test]
    fn new_lane_entries_default_to_unassigned_owner() {
        // 未割当は明示的に "unassigned"（contract 「未割当は unassigned を明示」）。
        let mut lane = HumanLane::new(4);
        lane.push(env("A"), "stripped-cot");
        assert_eq!(lane.current_owner(), "unassigned");
        let entry = lane.pending().next().expect("A pending");
        assert_eq!(entry.owner, "unassigned");
        assert!(lane.handoff_log().is_empty(), "no handoff has happened yet");
    }

    #[test]
    fn handoff_transfers_unacked_entries_owner_and_records_handoff() {
        // シフト交代: 未 ack item（AとB）の owner が次シフト担当者へ引き継がれ、handoff レコード
        // （前任者・後任者・引き継ぎ時刻・未 ack 残数）が追記される。エントリ自体は消えない
        // （人間レーンの中身を消さない＝contract の不変条件）。
        let mut lane = HumanLane::new(8);
        lane.push(env("A"), "stripped-cot");
        lane.push(env("B"), "stripped-cot");
        assert_eq!(lane.pending_len(), 2, "before handoff, entries present");

        let record = lane.handoff("operator-2");

        assert_eq!(record.previous_owner, "unassigned");
        assert_eq!(record.next_owner, "operator-2");
        assert_eq!(record.handoff_sequence, 0, "first handoff");
        assert_eq!(
            record.pending_at_handoff, 2,
            "A and B were pending at handoff"
        );

        // ハンドオフ後もエントリ数は変わらない（消えない）が owner が更新されている。
        assert_eq!(
            lane.pending_len(),
            2,
            "handoff must not consume the human lane"
        );
        for entry in lane.pending() {
            assert_eq!(
                entry.owner, "operator-2",
                "unacked entry must carry the new shift's owner after handoff"
            );
        }
        assert_eq!(lane.current_owner(), "operator-2");

        // handoff ログは追記専用で 1 件記録される。
        assert_eq!(lane.handoff_log().len(), 1);
        assert_eq!(lane.handoff_log()[0], record);
    }

    #[test]
    fn handoff_does_not_retroactively_change_owner_of_already_acked_entries() {
        // ack 済（review 済）エントリは引き継ぎ対象の "未 ack item" ではないため owner を
        // 書き換えない（handoff は未処理の残作業だけを次シフトへ渡す）。
        let mut lane = HumanLane::new(8);
        lane.push(env("A"), "stripped-cot");
        lane.push(env("B"), "stripped-cot");
        lane.ack(0); // A を ack 済に。

        lane.handoff("operator-2");

        let a = lane
            .entries
            .iter()
            .find(|e| e.sequence == 0)
            .expect("A still present (handoff does not delete entries)");
        assert_eq!(
            a.owner, "unassigned",
            "acked entry keeps its owner at the time it was acked"
        );
        let b = lane.pending().next().expect("B still pending");
        assert_eq!(b.owner, "operator-2", "unacked B is transferred");
    }

    #[test]
    fn multiple_handoffs_append_without_overwriting_prior_records() {
        // 複数回のシフト交代でログが追記され続ける（上書きしない＝過去の引き継ぎ履歴を消さない）。
        let mut lane = HumanLane::new(8);
        lane.push(env("A"), "stripped-cot");

        let first = lane.handoff("operator-2");
        lane.push(env("B"), "stripped-cot");
        let second = lane.handoff("operator-3");

        assert_eq!(lane.handoff_log().len(), 2, "both records retained");
        assert_eq!(lane.handoff_log()[0], first);
        assert_eq!(lane.handoff_log()[1], second);
        assert_eq!(second.previous_owner, "operator-2");
        assert_eq!(second.next_owner, "operator-3");
        assert_eq!(second.handoff_sequence, 1, "monotonically increasing");
        assert_eq!(
            second.pending_at_handoff, 2,
            "A (still unacked) and B are both pending at the second handoff"
        );

        // 両方とも最新の担当者へ更新されている（A は 2 回引き継がれた）。
        for entry in lane.pending() {
            assert_eq!(entry.owner, "operator-3");
        }
    }

    #[test]
    fn new_pushes_after_handoff_inherit_the_new_owner() {
        // ハンドオフ後に新規 push された item は、その時点の current_owner を引き継ぐ
        // （次シフト担当者が新規に受け取る item も unassigned のまま残らない）。
        let mut lane = HumanLane::new(8);
        lane.push(env("A"), "stripped-cot");
        lane.handoff("operator-2");
        lane.push(env("C"), "stripped-cot");

        let c = lane
            .entries
            .iter()
            .find(|e| e.sequence == 1)
            .expect("C present");
        assert_eq!(c.owner, "operator-2");
    }
}
