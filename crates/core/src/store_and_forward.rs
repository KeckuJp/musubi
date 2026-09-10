//! StoreAndForward — DDIL 滞留バッファ ＋ 因果順 drain ＋ ORDER_UNKNOWN 付与。
//!
//! 「store-and-forward・差分同期・原時刻/provenance 保持・stale/missing を MARK」
//! 「捨てるより遅らせる」の決定論的中核。
//!
//! **socket / net を一切持たない in-memory 決定論バッファ**。実搬送（bundle protocol 等）は
//! 別プロセスが担い、core は socket を持たない（memory-safety の blast radius を広げない）。
//! この構造体は [`EvidenceEnvelope`] を **消費するが変更しない**（sidecar に [`VectorClock`] を
//! 並走させる＝EvidenceEnvelope 不変＝golden digest 不変）。
//!
//! 不変条件（no-silent-drop）:
//! - `link` が [`LinkState::Down`] の間は **全件保存**（[`push`](StoreAndForwardBuffer::push) は
//!   決してパニック/ブロックしない＝常に [`PushOutcome`] を返す。ただしこれは
//!   **無条件の無制限保存ではない**: 容量超過は fail-open eviction、既知重複は拒否、として
//!   明示的・計測可能に扱う＝後述）。[`drain`](StoreAndForwardBuffer::drain) は空を返し
//!   pending を保持したまま。
//! - `link` が [`LinkState::Up`] に復旧したら **因果順**で全件を排出する。順序確定不能な並行群は
//!   捨てず `order_unknown=true` を付けて出す（ORDER_UNKNOWN は MARK レジャへ・捨てない）。
//!
//! honest scope（over-claim 禁止）: ここが扱うのは **自然な並行/損失**（自然な切断・reorder・
//! duplicate）。敵対的 replay/DoS/GPS spoof/equivocation は対象外である（CRDT が担うのは
//! 収束と可用性のみで、Byzantine 耐性は主張しない）。搬送層へのハンドオフ失敗時のリトライは
//! その搬送プロセスの責務であって、本構造体の外にある。
//!
//! ## bounded capacity ＋ fail-open eviction ＋ anti-replay window（既知重複の拒否）
//!
//! 容量無制限の `VecDeque` で `push` が決して失敗しない設計にすると、長期滞留下では
//! **自然に OOM→プロセス死**になる。これは fail-open（捨てるより遅らせる・失敗を第一級
//! オブジェクト化）と**正反対**の fail-silent なプロセス死である。replay/dedup ロジックが
//! 無ければ「既知 replay 受理=0」「window 内重複率=0」を検証する手段も無い。
//!
//! 本実装は 2 つの機構を追加する:
//! - **bounded capacity**: `capacity` を超えたら [`push`](StoreAndForwardBuffer::push) は
//!   **fail-open** で最古の未 drain envelope を退避（evict）し `Some(evicted)` を返す（呼び出し側が
//!   `MarkStatus::BufferSaturated` で MARK できる・黙って消えない・OOM でプロセスが死ぬのではなく
//!   古いものから明示的に手放す）。
//! - **anti-replay window**: 直近 `replay_window` 件の [`replay_key`]（**受信不変**な内容
//!   fingerprint）を憶えておき、同一 key の再 push は [`PushOutcome::RejectedDuplicate`] で
//!   拒否する（pending からだけでなく **drain 済でも window 内なら**検出＝「回線断→復旧後の
//!   再送」も捕捉する）。
//!
//! ### dedup キーは `content_digest` ではなく [`replay_key`]
//!
//! dedup を `content_digest`（= canonical_bytes の SHA-256）でキーにすると、canonical_bytes は
//! `Timestamps.received_at`・`time_confidence` を含むため、**同一フレームの再送（実運用の
//! replay）は受信のたびに digest が変わり一切検知できない**（「同一 sealed envelope をそのまま
//! 2 回 push」という縮退ケースしか捕まえられない）。
//! 本版は受信ごとに変わりうるフィールド（`received_at`・`time_confidence`・`confidence`・
//! MARK・seal/署名系フィールド）を**除いた** observation 実質内容＋claim 同一性から
//! [`replay_key`] を計算して照合する。`content_digest` / `canonical_bytes` 自体（署名対象・
//! golden digest）には一切触れない（独立した直列化＝golden digest pin 不変）。
//!
//! honest scope: これは **自然な重複**（同一 envelope の意図しない再送）の検出であって、
//! 暗号的な Byzantine 耐性・敵対的 equivocation 対策ではない（引き続き Phase B red-team）。
//! また `observed_at=None` の観測（例: MAVLink boot 相対時刻）で内容が完全一致する場合、
//! 「静止機体の正当な連続報告」と「replay」は原理的に区別できず window 内は重複扱いになる
//! （区別には機器側の単調カウンタ/nonce が要る＝Phase B）。

use crate::vector_clock::{CausalRelation, VectorClock};
use musubi_types::{ComObject, EvidenceEnvelope};
use sha2::{Digest, Sha256};
use std::collections::VecDeque;

/// 既定のバッファ容量（DENIED 長期滞留下でも無制限成長→OOM を防ぐための上限）。
///
/// 具体的な運用上限は後続 increment のキャパシティプランニングで調整するプレースホルダ値。
/// 本質は「**境界がある**こと」（無制限 `VecDeque` であること自体が問題である）。
pub const DEFAULT_CAPACITY: usize = 10_000;

/// 既定の anti-replay window サイズ（直近 push の [`replay_key`] を憶えておく件数の上限）。
pub const DEFAULT_REPLAY_WINDOW: usize = 4_096;

/// anti-replay dedup キー: envelope の**受信不変**な実質内容の SHA-256 fingerprint。
///
/// `content_digest`（= `canonical_bytes` の SHA-256）は `Timestamps.received_at`・
/// `time_confidence` を含むため、同一フレームの再送でも受信ごとに値が変わり replay 照合に
/// 使えない。本キーは以下**だけ**から計算する:
///
/// - **observation 実質内容**（[`ComObject::PlatformState`]）: `platform_id`（source 同一性）・
///   `position`・`mode`・`timestamps.observed_at`（機器が主張する観測時刻＝フレーム内容の一部。
///   replay は同一値を運び、真に新しい観測は前進する）。
/// - **claim 同一性**: `claim_id`（adapter が `platform_id`＋adapter slug から決定論で作る
///   受信不変 ID）。
///
/// **意図的に除外**（受信ごとに変わりうる／seal 状態依存のフィールド）:
/// `received_at`・`time_confidence`・`claim.confidence`・`claim.mark`（time_confidence 由来で
/// 変わりうる）・`classification`・`content_digest`・`signature`・署名系の予約 3 フィールド。
/// よって **seal 前後で同じ key** になる（dedup は seal 状態に依存しない）。
///
/// `canonical_bytes` とは**独立の直列化**（domain separation プレフィックス付き）で、
/// `content_digest`／golden digest pin には一切影響しない。直列化の各要素は `crate` の
/// 決定論 push ヘルパ（長さ前置 UTF-8・presence byte・bit-exact LE）を再利用する。
#[must_use]
pub fn replay_key(envelope: &EvidenceEnvelope) -> [u8; 32] {
    let mut buf = Vec::new();
    // domain separation: canonical_bytes（content_digest の入力）と絶対に混同されないように。
    buf.extend_from_slice(b"musubi/replay-key/v1");
    match &envelope.observation {
        ComObject::PlatformState(ps) => {
            buf.push(0x00); // variant tag（canonical_bytes と同じ割当・ただし独立の名前空間）
            crate::push_str(&mut buf, &ps.platform_id);
            match &ps.position {
                None => buf.push(0x00),
                Some(pos) => {
                    buf.push(0x01);
                    crate::push_position(&mut buf, pos);
                }
            }
            crate::push_opt_str(&mut buf, ps.mode.as_deref());
            // observed_at は観測内容の一部（replay は同一値を運ぶ）→ 含める。
            // received_at / time_confidence は受信ごとに変わる → 意図的に除外。
            crate::push_opt_i64(&mut buf, ps.timestamps.observed_at);
        }
        ComObject::HostStateObservation(observation) => {
            buf.push(0x01);
            crate::push_str(&mut buf, &observation.source_contract_id);
            crate::push_str(&mut buf, &observation.host_id);
            buf.extend_from_slice(&observation.sequence.to_le_bytes());
            crate::push_opt_i64(&mut buf, observation.timestamps.observed_at);
            // Host values are already part of the source record identity. Reuse the canonical
            // content encoder, while receipt-varying received_at/time_confidence remain excluded
            // above through the explicit observed-at prefix.
            let mut stable = observation.clone();
            stable.timestamps.received_at = 0;
            stable.timestamps.time_confidence = 0.0;
            crate::push_host_state_observation(&mut buf, &stable);
        }
    }
    crate::push_str(&mut buf, &envelope.claim.claim_id);
    Sha256::digest(&buf).into()
}

/// link の状態（DTN transport 層の接続可否）。
///
/// store-and-forward のゲート: `Up` なら drain 可能・`Down` なら滞留（no-silent-drop）。
/// 既定は `Up`（`StoreAndForwardBuffer::default` で接続済みから始める）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum LinkState {
    /// 転送可能（drain で因果順排出）。
    #[default]
    Up,
    /// 回線断 → バッファに push（no-silent-drop・全件保持）。
    Down,
}

/// バッファに積む単位（envelope 本体 ＋ 因果情報）。
///
/// `vc` はこの envelope が作られた時点の（送信側ノードの）vector clock スナップショット。
/// drain 時の因果順ソートと ORDER_UNKNOWN 検出に使う。**`EvidenceEnvelope` には vector clock を
/// 足さない**（canonical_bytes/golden digest を壊さないため sidecar に持つ＝設計確定）。
#[derive(Debug, Clone, PartialEq)]
pub struct BufferedEnvelope {
    pub envelope: EvidenceEnvelope,
    pub vc: VectorClock,
}

/// drain 後の 1 件。`order_unknown` が ORDER_UNKNOWN 付与の判断材料（side-channel フラグ）。
///
/// **他の全要素と `Concurrent`**（因果的に孤立）なら `true`。呼び出し側（fan-out 層）が
/// これを見て `MarkStatus::OrderUnknown` を MARK レジャへ計上する。**`EvidenceEnvelope` 本体は
/// 不変**（content_digest を再計算しない＝単一正本・golden digest pin を保つ）。
#[derive(Debug, Clone, PartialEq)]
pub struct DrainedItem {
    pub buffered: BufferedEnvelope,
    /// 他の全要素と `Concurrent`（因果的に孤立＝順序確定不能）なら `true`（到着順非依存・対称）。
    pub order_unknown: bool,
}

impl DrainedItem {
    /// この item の envelope への不変参照（呼び出し側が publish/fan-out に渡す）。
    #[must_use]
    pub fn envelope(&self) -> &EvidenceEnvelope {
        &self.buffered.envelope
    }
}

/// `push` の結果（no-silent-drop: 満杯/重複いずれも黙って消えず呼び出し側が
/// MARK/計数できる）。
#[derive(Debug, Clone, PartialEq)]
pub enum PushOutcome {
    /// 通常受理（滞留キューに追加しただけ・退避なし）。
    Accepted,
    /// 容量超過のため **fail-open**: 最古の未 drain envelope を退避（evict）して受理した
    /// （「捨てるより遅らせる」の限界に達した合図・呼び出し側が `MarkStatus::BufferSaturated`
    /// で MARK すべき対象）。
    AcceptedWithEviction(BufferedEnvelope),
    /// anti-replay window 内で同一 [`replay_key`]（受信不変な内容 fingerprint）を検出＝
    /// 重複として **拒否**した（バッファには積まれない・呼び出し側が「既知 replay」として
    /// 計数する対象）。received_at 等が違っていても同一内容の再送なら検出する。
    RejectedDuplicate,
}

impl PushOutcome {
    /// 容量超過で退避が発生したか。
    #[must_use]
    pub fn is_eviction(&self) -> bool {
        matches!(self, PushOutcome::AcceptedWithEviction(_))
    }

    /// 重複として拒否されたか。
    #[must_use]
    pub fn is_duplicate(&self) -> bool {
        matches!(self, PushOutcome::RejectedDuplicate)
    }
}

/// store-and-forward の in-memory 決定論バッファ。
///
/// socket / net を持たない。`link` が `Down` の間は全件保存し（no-silent-drop）、`Up` 復旧時に
/// 因果順で drain する。並行群は ORDER_UNKNOWN を付けて出す（捨てない）。
///
/// **bounded**（`capacity` 超過は fail-open で最古を退避）＋ **anti-replay window**
/// （直近の [`replay_key`]（受信不変な内容 fingerprint）を憶え、既知の再送を拒否）。
#[derive(Debug)]
pub struct StoreAndForwardBuffer {
    link: LinkState,
    /// 滞留キュー（envelope + その時点での送信側 vector clock のスナップショット）。
    pending: VecDeque<BufferedEnvelope>,
    /// drain した累計件数（no-silent-drop の件数保存を呼び出し側が検証する材料）。
    drained_total: usize,
    /// 滞留キューの容量上限（超過時は最古を fail-open で退避）。
    capacity: usize,
    /// anti-replay window: 直近 push を受理した [`replay_key`] の到着順履歴（重複検出用）。
    replay_window: VecDeque<[u8; 32]>,
    /// anti-replay window の上限件数（超過したら最古の記憶を忘れる＝これも bounded）。
    replay_window_cap: usize,
    /// window 内重複として拒否した累計件数（「既知 replay 受理=0」の検証材料）。
    duplicates_rejected: usize,
    /// 容量超過で退避（evict）した累計件数（黙って消えた件数ではなく計上された件数）。
    evicted_total: usize,
}

impl Default for StoreAndForwardBuffer {
    fn default() -> Self {
        Self::new(LinkState::default())
    }
}

impl StoreAndForwardBuffer {
    /// 初期 link 状態を指定して生成する（容量/replay window は既定値）。
    #[must_use]
    pub fn new(initial: LinkState) -> Self {
        Self::with_capacity(initial, DEFAULT_CAPACITY, DEFAULT_REPLAY_WINDOW)
    }

    /// 初期 link 状態 ＋ 容量 ＋ anti-replay window サイズを明示して生成する
    /// （テスト/運用チューニング用。小さい容量で eviction/dedup の挙動をピンポイントで確かめられる）。
    #[must_use]
    pub fn with_capacity(initial: LinkState, capacity: usize, replay_window_cap: usize) -> Self {
        Self {
            link: initial,
            pending: VecDeque::new(),
            drained_total: 0,
            capacity: capacity.max(1),
            replay_window: VecDeque::new(),
            replay_window_cap: replay_window_cap.max(1),
            duplicates_rejected: 0,
            evicted_total: 0,
        }
    }

    /// link 状態を更新する（DTN transport 層からのコールバック想定）。
    ///
    /// `Down` → `Up` 遷移後に [`drain`](Self::drain) を呼ぶと滞留分が因果順で排出される。
    pub fn set_link(&mut self, state: LinkState) {
        self.link = state;
    }

    /// 現在の link 状態。
    #[must_use]
    pub fn link(&self) -> LinkState {
        self.link
    }

    /// 滞留中（未 drain）の件数。no-silent-drop の件数保存検証に使う。
    #[must_use]
    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// これまでに drain した累計件数。`pending_len + drained_total` = push 総数（件数保存の
    /// 不変条件は `+ duplicates_rejected + evicted_total` を加えた形に一般化される
    /// ＝重複拒否/退避も「黙って消えた」わけではなく計上される）。
    #[must_use]
    pub fn drained_total(&self) -> usize {
        self.drained_total
    }

    /// 滞留キューの容量上限。
    #[must_use]
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// anti-replay window 内の重複として拒否した累計件数。
    #[must_use]
    pub fn duplicates_rejected(&self) -> usize {
        self.duplicates_rejected
    }

    /// 容量超過で fail-open 退避した累計件数（黙って消えた件数ではなく計上された件数）。
    #[must_use]
    pub fn evicted_total(&self) -> usize {
        self.evicted_total
    }

    /// envelope を push する（bounded ＋ anti-replay）。
    ///
    /// 1. **anti-replay**: [`replay_key`]（受信不変な内容 fingerprint・`content_digest` とは独立）
    ///    が直近 window 内に既にあれば [`PushOutcome::RejectedDuplicate`] を返し、**バッファには
    ///    積まない**。received_at・time_confidence が受信ごとに変わっていても同一内容の再送は
    ///    検出する（`content_digest` をキーにするとこれを検知できない）。
    ///    seal 前後を問わず全 envelope が dedup 対象（replay_key は seal 状態に依存しない）。
    /// 2. **bounded**: 通常受理後、`pending.len()` が `capacity` を超えたら最古（`pop_front`）を
    ///    fail-open で退避し [`PushOutcome::AcceptedWithEviction`] を返す（プロセス死ではなく
    ///    明示的に古いものから手放す）。
    /// 3. それ以外は [`PushOutcome::Accepted`]。
    ///
    /// `vc` はこの envelope の vector clock スナップショット（drain 時の因果順ソートに使う）。
    pub fn push(&mut self, envelope: EvidenceEnvelope, vc: VectorClock) -> PushOutcome {
        let key = replay_key(&envelope);
        if self.replay_window.contains(&key) {
            self.duplicates_rejected += 1;
            return PushOutcome::RejectedDuplicate;
        }
        self.remember_key(key);

        self.pending.push_back(BufferedEnvelope { envelope, vc });

        if self.pending.len() > self.capacity {
            // 最古（先頭）を fail-open で退避＝古いものから破棄（捨てるより遅らせるの限界）。
            let evicted = self
                .pending
                .pop_front()
                .expect("len() > capacity(>=1) implies pending is non-empty");
            self.evicted_total += 1;
            return PushOutcome::AcceptedWithEviction(evicted);
        }
        PushOutcome::Accepted
    }

    /// anti-replay window へ [`replay_key`] を記憶する（bounded・最古を忘れる）。
    fn remember_key(&mut self, key: [u8; 32]) {
        self.replay_window.push_back(key);
        if self.replay_window.len() > self.replay_window_cap {
            self.replay_window.pop_front();
        }
    }

    /// link が `Up` のとき、滞留 envelope を **因果順**で全件排出する。
    ///
    /// - link が `Down` → 空 `Vec` を返す（まだ送れない・pending は保持＝no-silent-drop）。
    /// - link が `Up` → pending を因果順ソートし、各要素に ORDER_UNKNOWN フラグを付けて返す。
    ///   並行（`Concurrent`）群は捨てず `order_unknown=true` で出す。
    ///
    /// **件数保存**: 返した件数だけ pending から取り除き `drained_total` に加算する。Down 時は
    /// 0 件取り除き（保持）。これにより `pending_len + drained_total` = 累計 push 数が常に成立。
    ///
    /// honest scope: 本メソッドは「因果順に並べ ORDER_UNKNOWN を付ける」までで、実際の DTN 層
    /// ハンドオフ（socket 送出）は別プロセスの責務（ここでは socket を一切触らない）。
    pub fn drain(&mut self) -> Vec<DrainedItem> {
        if self.link == LinkState::Down {
            return Vec::new();
        }
        let items: Vec<BufferedEnvelope> = self.pending.drain(..).collect();
        let n = items.len();
        let sorted = causal_sort(items);
        self.drained_total += n;
        annotate_order_unknown(sorted)
    }
}

/// 因果順ソート（**安定トポロジカルソート**・決定論）。
///
/// `HappensBefore`（半順序）を**必ず**尊重する: A が B に happens-before なら A を B より前に出す。
/// 未出力の中に「自分より happens-before な先行要素」が残っていない要素のうち、最も早く到着した
/// （index 最小）ものを選び続ける（到着順 tiebreak で安定・並行要素はどの到着順でも決定論）。
///
/// ⚠️ `sort_by` を `Concurrent→Equal` で使ってはならない: `Concurrent` は同値関係でなく
/// `HappensBefore` に対し非推移的（A∥B・B∥C でも A&lt;C）なので strict-weak-ordering 契約を破り、
/// 並行要素が因果連鎖の間に割り込むと std sort が `HappensBefore` ペアの順序を**反転**しうる
/// （independent verifier が arrival-order permutation で実証した major bug）。O(n²) だが DDIL
/// バッファは有限件で実用十分。`Concurrent` 群は後段 [`annotate_order_unknown`] で ORDER_UNKNOWN 化。
fn causal_sort(items: Vec<BufferedEnvelope>) -> Vec<BufferedEnvelope> {
    let n = items.len();
    let clocks: Vec<VectorClock> = items.iter().map(|b| b.vc.clone()).collect();
    let mut slots: Vec<Option<BufferedEnvelope>> = items.into_iter().map(Some).collect();
    let mut emitted = vec![false; n];
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let mut pick = None;
        for i in 0..n {
            if emitted[i] {
                continue;
            }
            // 未出力の中に「自分より happens-before な要素」が残っていなければ ready。
            let has_pending_pred = (0..n).any(|j| {
                j != i
                    && !emitted[j]
                    && matches!(
                        clocks[j].relation(&clocks[i]),
                        CausalRelation::HappensBefore
                    )
            });
            if !has_pending_pred {
                pick = Some(i); // index 昇順で最初の ready＝到着順 tiebreak（安定）。
                break;
            }
        }
        // vector clock の HappensBefore は非循環なので ready 要素が必ず存在する。
        let idx = pick.expect("causal topological sort: a ready element must exist (acyclic)");
        emitted[idx] = true;
        out.push(slots[idx].take().expect("picked slot present"));
    }
    out
}

/// 各要素に ORDER_UNKNOWN フラグを付ける（**到着順非依存・対称**・決定論）。
///
/// `order_unknown = true` ⇔ その要素が**他の全要素と `Concurrent`**（＝どの要素とも
/// `HappensBefore` 関係を持たない「因果的に孤立した floater」）。
/// - 純粋な並行ペア {A∥B} → 双方が孤立 → **両方** true（multi-value 両 MARK）。
/// - 因果連鎖 {A&lt;B&lt;C} → 各要素は隣接と happens-before → どれも孤立でない → 全て false。
/// - 連鎖＋並行 intruder {A&lt;C, B∥both} → B のみ孤立 → B だけ true・A/C は false（連鎖は確定）。
///
/// 旧実装は「直前 1 個との Concurrent」を見る running-base で、**到着順に依存**し並行ペアの
/// 片方しか付かない major bug があった（independent verifier 2 名が permutation で実証）。本実装は
/// 集合全体の pairwise 関係だけで決まるので到着順に依らず安定。
fn annotate_order_unknown(items: Vec<BufferedEnvelope>) -> Vec<DrainedItem> {
    let n = items.len();
    let clocks: Vec<VectorClock> = items.iter().map(|b| b.vc.clone()).collect();
    let mut out = Vec::with_capacity(n);
    for (i, item) in items.into_iter().enumerate() {
        // 他の全要素と Concurrent（= happens-before 関係が一切無い）なら因果的に孤立＝ORDER_UNKNOWN。
        let order_unknown = n > 1
            && (0..n).all(|j| {
                j == i || matches!(clocks[i].relation(&clocks[j]), CausalRelation::Concurrent)
            });
        out.push(DrainedItem {
            buffered: item,
            order_unknown,
        });
    }
    out
}

#[cfg(test)]
mod tests {
    //! store-and-forward の no-silent-drop（件数保存）・因果順 drain・
    //! ORDER_UNKNOWN 付与（並行→付く・happens-before→付かない）の決定論検証。

    use super::{DEFAULT_CAPACITY, LinkState, PushOutcome, StoreAndForwardBuffer, replay_key};
    use crate::vector_clock::VectorClock;
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
    };

    /// 観測 envelope を作る（platform_id でどの観測か識別）。
    fn obs(platform_id: &str) -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: platform_id.to_string(),
                position: Some(Position {
                    lat_deg: 35.0,
                    lon_deg: 139.0,
                    alt_m: Some(1.0),
                }),
                mode: None,
                timestamps: Timestamps {
                    observed_at: Some(10),
                    received_at: 100,
                    time_confidence: 0.9,
                },
                platform_domain: musubi_types::PlatformDomain::Unknown,
            }),
            claim: Claim {
                claim_id: format!("c-{platform_id}"),
                confidence: 0.8,
                mark: Mark {
                    status: MarkStatus::Ok,
                    reason_code: "normalized".to_string(),
                    provenance: vec![format!("source:{platform_id}")],
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

    fn pid(env: &EvidenceEnvelope) -> &str {
        let ComObject::PlatformState(ps) = &env.observation else {
            panic!("expected PlatformState")
        };
        &ps.platform_id
    }

    // === must_prove ①: 回線断→復旧で全件を順序保って配送（no-silent-drop・件数保存） ===

    #[test]
    fn down_then_up_delivers_all_in_causal_order() {
        // alpha が 3 件を逐次生成（vc が {alpha:1}<{alpha:2}<{alpha:3}）。回線断中に溜め、
        // 復旧で drain → 全 3 件が因果順（E1,E2,E3）で出る・1 件も落ちない。
        let mut buf = StoreAndForwardBuffer::new(LinkState::Down);
        buf.push(obs("E1"), VectorClock::single("alpha", 1));
        buf.push(obs("E2"), VectorClock::single("alpha", 2));
        buf.push(obs("E3"), VectorClock::single("alpha", 3));

        // Down 中は drain しても何も出ない（pending に全件保持＝no-silent-drop）。
        assert!(buf.drain().is_empty());
        assert_eq!(buf.pending_len(), 3);
        assert_eq!(buf.drained_total(), 0);

        // 復旧 → 全件因果順で排出。
        buf.set_link(LinkState::Up);
        let drained = buf.drain();
        assert_eq!(drained.len(), 3, "all buffered items must be delivered");
        assert_eq!(pid(drained[0].envelope()), "E1");
        assert_eq!(pid(drained[1].envelope()), "E2");
        assert_eq!(pid(drained[2].envelope()), "E3");
        // 件数保存: pending=0・drained_total=3（push 総数と一致）。
        assert_eq!(buf.pending_len(), 0);
        assert_eq!(buf.drained_total(), 3);
        // 因果連鎖は ORDER_UNKNOWN が付かない（全て happens-before で確定）。
        assert!(drained.iter().all(|d| !d.order_unknown));
    }

    #[test]
    fn out_of_order_arrival_is_sorted_by_causality() {
        // 到着順を逆（E3,E1,E2）にしても、因果順ソートで E1,E2,E3 に並ぶ（決定論）。
        let mut buf = StoreAndForwardBuffer::new(LinkState::Down);
        buf.push(obs("E3"), VectorClock::single("alpha", 3));
        buf.push(obs("E1"), VectorClock::single("alpha", 1));
        buf.push(obs("E2"), VectorClock::single("alpha", 2));
        buf.set_link(LinkState::Up);
        let drained = buf.drain();
        assert_eq!(pid(drained[0].envelope()), "E1");
        assert_eq!(pid(drained[1].envelope()), "E2");
        assert_eq!(pid(drained[2].envelope()), "E3");
    }

    #[test]
    fn count_preserved_across_multiple_drain_cycles() {
        // 複数の断/復旧サイクルでも push 総数 = pending + drained_total が常に成立（件数保存）。
        let mut buf = StoreAndForwardBuffer::new(LinkState::Up);
        buf.push(obs("A"), VectorClock::single("alpha", 1));
        let d1 = buf.drain(); // Up: 即排出
        assert_eq!(d1.len(), 1);

        buf.set_link(LinkState::Down);
        buf.push(obs("B"), VectorClock::single("alpha", 2));
        buf.push(obs("C"), VectorClock::single("alpha", 3));
        assert!(buf.drain().is_empty()); // Down: 保持
        assert_eq!(buf.pending_len(), 2);

        buf.set_link(LinkState::Up);
        let d2 = buf.drain();
        assert_eq!(d2.len(), 2);
        // 累計: push 3 件 = drained_total 3 + pending 0。
        assert_eq!(buf.drained_total(), 3);
        assert_eq!(buf.pending_len(), 0);
    }

    // === must_prove ②: 並行 vector clock → ORDER_UNKNOWN・因果連鎖 → 順序保持 ===

    #[test]
    fn concurrent_clocks_get_order_unknown_and_are_not_dropped() {
        // golden_example（VECTORCLOCK 設計）: alpha の 3 連鎖 ＋ beta の独立 1 件。
        // beta(F1) は alpha 連鎖と Concurrent → ORDER_UNKNOWN・ただし捨てない（no-silent-drop）。
        let mut buf = StoreAndForwardBuffer::new(LinkState::Down);
        buf.push(obs("E1"), VectorClock::single("alpha", 1));
        buf.push(obs("E2"), VectorClock::single("alpha", 2));
        buf.push(obs("E3"), VectorClock::single("alpha", 3));
        buf.push(obs("F1"), VectorClock::single("beta", 1)); // 独立ノード
        buf.set_link(LinkState::Up);
        let drained = buf.drain();

        // 全 4 件が出る（F1 を捨てない＝no-silent-drop）。
        assert_eq!(drained.len(), 4);
        // F1 を探す。
        let f1 = drained
            .iter()
            .find(|d| pid(d.envelope()) == "F1")
            .expect("F1 must be delivered, not dropped");
        assert!(
            f1.order_unknown,
            "concurrent (beta) item must be flagged ORDER_UNKNOWN"
        );
        // alpha 連鎖（E1/E2/E3）は因果確定 → ORDER_UNKNOWN なし。
        for tag in ["E1", "E2", "E3"] {
            let e = drained
                .iter()
                .find(|d| pid(d.envelope()) == tag)
                .expect("alpha item present");
            assert!(
                !e.order_unknown,
                "causally-ordered alpha item {tag} must NOT be ORDER_UNKNOWN"
            );
        }
    }

    #[test]
    fn two_concurrent_singletons_both_get_order_unknown_pair() {
        // 2 つの独立ノードの単発観測（{alpha:1} と {beta:1}）は互いに Concurrent＝双方が
        // 因果的に孤立 → **両方** ORDER_UNKNOWN（到着順非依存・対称＝並行競合は multi-value で
        // 両 Claim を保持し双方に MARK する）。multi-value 保持: 両方とも drain で出る。
        let mut buf = StoreAndForwardBuffer::new(LinkState::Down);
        buf.push(obs("Oa"), VectorClock::single("alpha", 1));
        buf.push(obs("Ob"), VectorClock::single("beta", 1));
        buf.set_link(LinkState::Up);
        let drained = buf.drain();
        assert_eq!(
            drained.len(),
            2,
            "both concurrent claims are kept (multi-value)"
        );
        assert!(
            drained.iter().all(|d| d.order_unknown),
            "BOTH isolated concurrent claims must be ORDER_UNKNOWN (symmetric)"
        );
    }

    #[test]
    fn concurrent_intruder_preserves_causal_order_and_flags_for_all_arrivals() {
        // verify 指摘（major×2）の回帰: A({alpha:1}) < C({alpha:2})・B({beta:1}) は両者と Concurrent。
        // **どの到着順でも** (1) A は必ず C より前（causal order 不変・旧 sort_by は arrival
        // [C,B,A] で C を A より前に出す違反があった） (2) 孤立した B だけが ORDER_UNKNOWN・
        // A/C は因果確定で flag なし（旧 running-base は到着順で A/C を誤 flag）。
        let mk = |tag: &str| -> (EvidenceEnvelope, VectorClock) {
            match tag {
                "A" => (obs("A"), VectorClock::single("alpha", 1)),
                "B" => (obs("B"), VectorClock::single("beta", 1)),
                "C" => (obs("C"), VectorClock::single("alpha", 2)),
                other => panic!("unexpected tag {other}"),
            }
        };
        for arrival in [
            ["A", "B", "C"],
            ["A", "C", "B"],
            ["B", "A", "C"],
            ["B", "C", "A"],
            ["C", "A", "B"],
            ["C", "B", "A"],
        ] {
            let mut buf = StoreAndForwardBuffer::new(LinkState::Down);
            for tag in arrival {
                let (e, vc) = mk(tag);
                buf.push(e, vc);
            }
            buf.set_link(LinkState::Up);
            let drained = buf.drain();
            assert_eq!(drained.len(), 3, "all kept for arrival {arrival:?}");
            let pos = |t: &str| {
                drained
                    .iter()
                    .position(|d| pid(d.envelope()) == t)
                    .expect("present")
            };
            assert!(
                pos("A") < pos("C"),
                "causal order A<C must hold for arrival {arrival:?}"
            );
            let flagged: Vec<&str> = drained
                .iter()
                .filter(|d| d.order_unknown)
                .map(|d| pid(d.envelope()))
                .collect();
            assert_eq!(
                flagged,
                vec!["B"],
                "only the isolated intruder B is ORDER_UNKNOWN for arrival {arrival:?}"
            );
        }
    }

    #[test]
    fn happens_before_chain_preserves_order_without_order_unknown() {
        // 純粋な因果連鎖（同一 source の逐次）は全て順序確定・ORDER_UNKNOWN ゼロ。
        let mut buf = StoreAndForwardBuffer::new(LinkState::Up);
        for i in 1..=5 {
            buf.push(obs(&format!("E{i}")), VectorClock::single("alpha", i));
        }
        let drained = buf.drain();
        assert_eq!(drained.len(), 5);
        assert!(
            drained.iter().all(|d| !d.order_unknown),
            "a happens-before chain must have zero ORDER_UNKNOWN"
        );
        // 順序も保たれる。
        for (i, d) in drained.iter().enumerate() {
            assert_eq!(pid(d.envelope()), format!("E{}", i + 1));
        }
    }

    // === 不変条件: drain は EvidenceEnvelope を変更しない（content_digest 不変＝単一正本） ===

    #[test]
    fn drain_does_not_mutate_envelope_digest() {
        // sidecar 設計の確証: sealed envelope を push→drain しても content_digest が変わらない。
        let sealed = crate::seal_digest(obs("E1"));
        let before = sealed.content_digest;
        let mut buf = StoreAndForwardBuffer::new(LinkState::Up);
        buf.push(sealed, VectorClock::single("alpha", 1));
        let drained = buf.drain();
        assert_eq!(
            drained[0].envelope().content_digest,
            before,
            "store-and-forward must not re-seal / mutate the envelope"
        );
    }

    #[test]
    fn empty_buffer_drain_is_empty_and_preserves_counts() {
        let mut buf = StoreAndForwardBuffer::new(LinkState::Up);
        assert!(buf.drain().is_empty());
        assert_eq!(buf.pending_len(), 0);
        assert_eq!(buf.drained_total(), 0);
    }

    // === bounded capacity（fail-open eviction）＋ anti-replay window（dedup） ===

    /// `StoreAndForwardBuffer::new` が実際に [`DEFAULT_CAPACITY`] を使って bounded になること
    /// の oracle。上の eviction/no-evict テストは実行速度のため `with_capacity(..., 2 or 3, ...)`
    /// の小容量で機構（bounded＋fail-open）だけを検証しており、**既定値そのもの**（`10_000`・
    /// 数時間の断を前提にしたブラケット placeholder）には oracle が付かない。運用定数に oracle が
    /// 無いと、値のサイレントな変更が誰にも気づかれずに通る。本テストは `new()` が確かに
    /// `DEFAULT_CAPACITY` を配線しており、その値が `10_000` と一致し続けることを固定する。
    ///
    /// honest scope: これは **配線の正しさ**のみを検証する。`10_000` という数値自体の妥当性
    /// （断時間・入力レートに対する十分性）は未検証のブラケット placeholder のままであり、
    /// 本テストはそれを保証しない。
    #[test]
    fn default_capacity_matches_documented_placeholder_value() {
        assert_eq!(
            DEFAULT_CAPACITY, 10_000,
            "DEFAULT_CAPACITY must match the documented bracket placeholder value"
        );
        let buf = StoreAndForwardBuffer::new(LinkState::Down);
        assert_eq!(
            buf.capacity(),
            DEFAULT_CAPACITY,
            "StoreAndForwardBuffer::new must wire DEFAULT_CAPACITY (not silently diverge from it)"
        );
    }

    #[test]
    fn push_within_capacity_returns_accepted_and_never_evicts() {
        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Down, 3, 10);
        for i in 1..=3 {
            let outcome = buf.push(obs(&format!("E{i}")), VectorClock::single("alpha", i));
            assert_eq!(outcome, PushOutcome::Accepted);
        }
        assert_eq!(buf.pending_len(), 3);
        assert_eq!(buf.evicted_total(), 0);
    }

    #[test]
    fn push_over_capacity_fail_opens_evicting_oldest_not_oom_not_silent() {
        // DENIED 長期滞留の再現: 容量 2 のバッファへ 3 件 push すると、無制限成長（OOM 相当）でも
        // 黙ってブロックする（fail-silent）でもなく、最古（E1）を明示的に退避して受理する。
        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Down, 2, 10);
        assert_eq!(
            buf.push(obs("E1"), VectorClock::single("alpha", 1)),
            PushOutcome::Accepted
        );
        assert_eq!(
            buf.push(obs("E2"), VectorClock::single("alpha", 2)),
            PushOutcome::Accepted
        );
        let outcome = buf.push(obs("E3"), VectorClock::single("alpha", 3));
        match outcome {
            PushOutcome::AcceptedWithEviction(evicted) => {
                assert_eq!(pid(&evicted.envelope), "E1", "oldest must be evicted first");
            }
            other => panic!("expected AcceptedWithEviction, got {other:?}"),
        }
        // 容量は超えない（E2, E3 のみ滞留）・退避は計上される（黙って消えない）。
        assert_eq!(buf.pending_len(), 2);
        assert_eq!(buf.evicted_total(), 1);
        buf.set_link(LinkState::Up);
        assert_eq!(
            pid(buf.drain().first().expect("E2 present").envelope()),
            "E2",
            "the surviving oldest (E2) must still drain in causal order"
        );
    }

    #[test]
    fn duplicate_sealed_envelope_is_rejected_within_replay_window() {
        // anti-replay: 同一 sealed envelope をそのまま 2 回 push する縮退ケース（replay_key も
        // 当然一致する）は 2 回目以降拒否される。
        let sealed = crate::seal_digest(obs("E1"));
        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Down, 100, 100);
        assert_eq!(
            buf.push(sealed.clone(), VectorClock::single("alpha", 1)),
            PushOutcome::Accepted
        );
        let replay = buf.push(sealed, VectorClock::single("alpha", 2));
        assert_eq!(replay, PushOutcome::RejectedDuplicate);
        // 重複は pending に積まれない（1 件のみ滞留）が、拒否件数として計上される。
        assert_eq!(buf.pending_len(), 1);
        assert_eq!(buf.duplicates_rejected(), 1);
    }

    #[test]
    fn duplicate_is_detected_even_after_the_original_has_already_drained() {
        // window は pending だけでなく drain 済でも効く（回線復旧後の再送を捕捉する）。
        let sealed = crate::seal_digest(obs("E1"));
        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Up, 100, 100);
        assert_eq!(
            buf.push(sealed.clone(), VectorClock::single("alpha", 1)),
            PushOutcome::Accepted
        );
        let drained = buf.drain();
        assert_eq!(drained.len(), 1, "E1 drains immediately (link Up)");
        // E1 は既に drain 済＝pending は空だが、window にはまだ digest が残っている。
        assert_eq!(buf.pending_len(), 0);
        let replay = buf.push(sealed, VectorClock::single("alpha", 2));
        assert_eq!(
            replay,
            PushOutcome::RejectedDuplicate,
            "a resend of an already-drained envelope must still be caught"
        );
    }

    #[test]
    fn dedup_no_longer_depends_on_sealing_unsealed_replay_is_rejected() {
        // content_digest=None（未 seal）を「fingerprint 不能＝dedup 対象外（常に受理）」と
        // すると seal 有無が抜け道になる。replay_key は seal 状態に依存しないため、
        // 未 seal でも同一内容の再送は 2 回目以降拒否される（seal 有無の抜け道を残さない）。
        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Down, 100, 100);
        assert_eq!(
            buf.push(obs("same-tag"), VectorClock::single("alpha", 1)),
            PushOutcome::Accepted
        );
        for _ in 0..2 {
            let outcome = buf.push(obs("same-tag"), VectorClock::single("alpha", 1));
            assert_eq!(outcome, PushOutcome::RejectedDuplicate);
        }
        assert_eq!(buf.pending_len(), 1);
        assert_eq!(buf.duplicates_rejected(), 2);
    }

    // === replay_key は受信時刻に依存しない（実運用 replay の検知） ===

    #[test]
    fn replay_with_different_received_at_is_rejected() {
        // 実運用の replay（同一フレームの再送）は受信のたびに received_at（と time_confidence
        // 評価）が変わる。canonical_bytes は received_at を含むため content_digest は毎回異なり、
        // digest ベースの dedup はこれを一切検知できない。replay_key ベースの dedup は 2 回目を
        // RejectedDuplicate にする。
        let first = crate::seal_digest(obs("E1"));

        // 同一の観測内容（platform_id・position・mode・observed_at）だが受信メタだけが違う再送。
        let mut resend = obs("E1");
        {
            let ComObject::PlatformState(ps) = &mut resend.observation else {
                panic!("expected PlatformState")
            };
            ps.timestamps.received_at = 200; // 1 回目は 100（obs() 既定）
            ps.timestamps.time_confidence = 0.7; // 受信ごとに変わりうる評価も違える（1 回目は 0.9）
        }
        let second = crate::seal_digest(resend);

        // 前提の確認: content_digest は互いに異なる＝旧キーでは重複に見えない（欠陥の核心）。
        assert_ne!(
            first.content_digest, second.content_digest,
            "precondition: received_at difference must change content_digest \
             (this is exactly why the old digest-based dedup missed real replays)"
        );

        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Down, 100, 100);
        assert_eq!(
            buf.push(first, VectorClock::single("alpha", 1)),
            PushOutcome::Accepted
        );
        assert_eq!(
            buf.push(second, VectorClock::single("alpha", 2)),
            PushOutcome::RejectedDuplicate,
            "a real-world replay (same content, later receipt time) must be rejected"
        );
        assert_eq!(buf.pending_len(), 1);
        assert_eq!(buf.duplicates_rejected(), 1);
    }

    #[test]
    fn genuinely_new_observation_from_same_source_is_not_deduplicated() {
        // 偽陽性ガード: 同じ機体の「真に新しい観測」（observed_at と position が前進）は
        // replay ではない → 受理される（replay_key は観測の実質内容を区別する）。
        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Down, 100, 100);
        assert_eq!(
            buf.push(
                crate::seal_digest(obs("E1")),
                VectorClock::single("alpha", 1)
            ),
            PushOutcome::Accepted
        );
        let mut next = obs("E1");
        {
            let ComObject::PlatformState(ps) = &mut next.observation else {
                panic!("expected PlatformState")
            };
            ps.timestamps.observed_at = Some(20); // obs() 既定は Some(10) — 観測時刻が前進
            ps.timestamps.received_at = 200;
            ps.position.as_mut().expect("pos").lat_deg = 35.001; // 位置も前進
        }
        assert_eq!(
            buf.push(crate::seal_digest(next), VectorClock::single("alpha", 2)),
            PushOutcome::Accepted,
            "a genuinely new observation must not be flagged as replay"
        );
        assert_eq!(buf.pending_len(), 2);
        assert_eq!(buf.duplicates_rejected(), 0);
    }

    #[test]
    fn replay_key_is_receipt_invariant_and_seal_independent() {
        // replay_key の直接 oracle:
        // (1) seal 前後で不変（content_digest / signature に依存しない）。
        let unsealed = obs("E1");
        let sealed = crate::seal_digest(obs("E1"));
        assert_eq!(
            replay_key(&unsealed),
            replay_key(&sealed),
            "replay_key must not depend on sealing state"
        );
        // (2) 受信ごとに変わりうるフィールド（received_at / time_confidence / confidence）に不変。
        let mut later = obs("E1");
        {
            let ComObject::PlatformState(ps) = &mut later.observation else {
                panic!("expected PlatformState")
            };
            ps.timestamps.received_at = 999_999;
            ps.timestamps.time_confidence = 0.01;
        }
        later.claim.confidence = 0.11;
        assert_eq!(
            replay_key(&obs("E1")),
            replay_key(&later),
            "replay_key must be invariant to receipt-varying fields"
        );
        // (3) 実質内容（platform_id / position / observed_at）が違えば key も違う。
        assert_ne!(replay_key(&obs("E1")), replay_key(&obs("E2")));
        let mut moved = obs("E1");
        {
            let ComObject::PlatformState(ps) = &mut moved.observation else {
                panic!("expected PlatformState")
            };
            ps.position.as_mut().expect("pos").lat_deg = 36.0;
        }
        assert_ne!(replay_key(&obs("E1")), replay_key(&moved));
        let mut newer = obs("E1");
        {
            let ComObject::PlatformState(ps) = &mut newer.observation else {
                panic!("expected PlatformState")
            };
            ps.timestamps.observed_at = Some(11);
        }
        assert_ne!(replay_key(&obs("E1")), replay_key(&newer));
    }

    #[test]
    fn replay_window_forgets_the_oldest_key_once_its_own_cap_is_exceeded() {
        // window 自体も bounded: window_cap=1 なら 2 件目の replay_key を憶えた時点で 1 件目の
        // key は忘れられ、1 件目と同じ内容の 3 回目 push はもう「新規」として受理される。
        let e1 = crate::seal_digest(obs("E1"));
        let e2 = crate::seal_digest(obs("E2"));
        let mut buf = StoreAndForwardBuffer::with_capacity(LinkState::Down, 100, 1);
        assert_eq!(
            buf.push(e1.clone(), VectorClock::single("alpha", 1)),
            PushOutcome::Accepted
        );
        assert_eq!(
            buf.push(e2, VectorClock::single("alpha", 2)),
            PushOutcome::Accepted,
            "distinct digest, still accepted"
        );
        // window_cap=1 なので e1 の key はもう window に無い＝再 push は重複判定されない。
        assert_eq!(
            buf.push(e1, VectorClock::single("alpha", 3)),
            PushOutcome::Accepted,
            "e1's replay key fell out of the bounded replay window"
        );
    }
}
