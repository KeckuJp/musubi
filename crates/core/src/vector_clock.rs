//! VectorClock — per-source 論理カウンタによる部分順序（partial-order）。
//!
//! 因果は partial-order として保持し、並行で並べられない事象には ORDER_UNKNOWN を MARK する。
//! その決定論的な基盤がこのモジュールである（収束と可用性そのものは CRDT 側の責務）。
//!
//! **net/io 不要・in-memory・決定論**。`BTreeMap` を使うことで挿入順に依存しない決定論的な
//! 比較・反復を保証する（`HashMap` は SipHash のランダム seed で挿入順が実行ごとに変わりうる）。
//!
//! honest scope（over-claim 禁止）: VectorClock は **Byzantine 耐性を持たない**。
//! 同一 source が矛盾する clock を意図的に発信する equivocation・compromised node からの偽注入は
//! 本機構の範囲外であり、暗号 identity＋per-source 鍵が担う領域である。
//! ここが扱うのは **自然な並行/損失**（自然な切断・reorder・duplicate）の因果整理のみ。

use std::collections::{BTreeMap, BTreeSet};

/// source ごとの論理カウンタ。`BTreeMap` で決定論的な全順序比較・直列化を保証する。
///
/// `source_id` は `String`（例: `"usv-001"`・`"uav-03"`・`"musubi-node-alpha"`）。
/// カウンタは `u64`（292 年 × 毎秒 10^9 件でもオーバーフローしない実用値）。`i64` だと負に
/// なりうり「0 から始まるカウンタ」の意味論が壊れるため符号なしを採る。
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct VectorClock(pub BTreeMap<String, u64>);

/// 因果関係の分類（Lamport clock では区別できない `Concurrent` が ORDER_UNKNOWN の核心）。
///
/// Lamport 1978（happens-before の定義）＋ Mattern 1988（Concurrent 分類の起源）に基づく。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CausalRelation {
    /// `self` < `other`（`self` の全成分 ≤ `other` かつ少なくとも 1 つ <）。
    HappensBefore,
    /// `self` > `other`（`other` が `self` より前）。
    HappensAfter,
    /// 全成分が完全一致。
    Equal,
    /// どちらでもない＝並行（`ORDER_UNKNOWN` の根拠）。
    Concurrent,
}

impl VectorClock {
    /// 空クロック（新規 source 登録前）。
    #[must_use]
    pub fn new() -> Self {
        Self(BTreeMap::new())
    }

    /// 単一 source・単一カウンタの clock を作る簡便コンストラクタ（テスト/呼び出し側用）。
    #[must_use]
    pub fn single(source_id: impl Into<String>, count: u64) -> Self {
        let mut m = BTreeMap::new();
        m.insert(source_id.into(), count);
        Self(m)
    }

    /// source のカウンタをインクリメントする（イベント送信前に呼ぶ）。
    ///
    /// 存在しない source は 0 から始める。`saturating_add` でオーバーフローは飽和させる
    /// （実用上到達しないが、「失敗を捨てない」の精神でパニックを避ける）。
    pub fn increment(&mut self, source_id: &str) {
        let c = self.0.entry(source_id.to_string()).or_insert(0);
        *c = c.saturating_add(1);
    }

    /// 指定 source の現在カウンタ（未登録は 0）。
    #[must_use]
    pub fn get(&self, source_id: &str) -> u64 {
        self.0.get(source_id).copied().unwrap_or(0)
    }

    /// 受信した他 clock とのマージ（各成分の max）。
    ///
    /// 受信側がイベントを処理する前に呼ぶ（Lamport/vector clock の標準手順）。
    pub fn merge(&mut self, other: &VectorClock) {
        for (k, &v) in &other.0 {
            let e = self.0.entry(k.clone()).or_insert(0);
            if v > *e {
                *e = v;
            }
        }
    }

    /// 因果関係の比較（決定論・クローン不要・O(n) n=source 数）。
    ///
    /// 両 clock の全 key を合算し、各成分を比較する。未登録成分は 0 とみなす。
    #[must_use]
    pub fn relation(&self, other: &VectorClock) -> CausalRelation {
        let keys: BTreeSet<&String> = self.0.keys().chain(other.0.keys()).collect();
        let mut self_lt = false; // self に other より小さい成分がある
        let mut self_gt = false; // self に other より大きい成分がある
        for k in keys {
            let a = self.0.get(k).copied().unwrap_or(0);
            let b = other.0.get(k).copied().unwrap_or(0);
            if a < b {
                self_lt = true;
            }
            if a > b {
                self_gt = true;
            }
        }
        match (self_lt, self_gt) {
            (false, false) => CausalRelation::Equal,
            (true, false) => CausalRelation::HappensBefore,
            (false, true) => CausalRelation::HappensAfter,
            (true, true) => CausalRelation::Concurrent,
        }
    }

    /// `other` と並行（`Concurrent`）か。ORDER_UNKNOWN 判定の述語。
    #[must_use]
    pub fn is_concurrent_with(&self, other: &VectorClock) -> bool {
        matches!(self.relation(other), CausalRelation::Concurrent)
    }
}

#[cfg(test)]
mod tests {
    //! VectorClock の因果分類（HappensBefore/After/Equal/Concurrent）の決定論検証。
    //! Concurrent（Lamport では区別不能な並行）を正確に検出できることがこの機構の要求である。

    use super::{CausalRelation, VectorClock};

    #[test]
    fn equal_clocks_are_equal() {
        let a = VectorClock::single("alpha", 3);
        let b = VectorClock::single("alpha", 3);
        assert_eq!(a.relation(&b), CausalRelation::Equal);
        // 空クロック同士も Equal。
        assert_eq!(
            VectorClock::new().relation(&VectorClock::new()),
            CausalRelation::Equal
        );
    }

    #[test]
    fn sequential_same_source_is_happens_before() {
        // {alpha:1} < {alpha:2}（同一 source の逐次 = 因果確定）。
        let a = VectorClock::single("alpha", 1);
        let b = VectorClock::single("alpha", 2);
        assert_eq!(a.relation(&b), CausalRelation::HappensBefore);
        assert_eq!(b.relation(&a), CausalRelation::HappensAfter);
    }

    #[test]
    fn disjoint_sources_are_concurrent() {
        // 核: {alpha:1} と {beta:1} は incomparable = Concurrent。
        // Lamport clock ではどちらかが必ず前に見えてしまう（並行を潰す）が、vector clock は
        // 並行を Concurrent として保持する。これが vector clock を選ぶ理由である。
        let a = VectorClock::single("alpha", 1);
        let b = VectorClock::single("beta", 1);
        assert_eq!(a.relation(&b), CausalRelation::Concurrent);
        assert_eq!(b.relation(&a), CausalRelation::Concurrent);
        assert!(a.is_concurrent_with(&b));
    }

    #[test]
    fn partial_overlap_can_be_concurrent() {
        // {alpha:2, beta:1} vs {alpha:1, beta:2}: 片方が大きく片方が小さい = Concurrent。
        let mut a = VectorClock::new();
        a.0.insert("alpha".to_string(), 2);
        a.0.insert("beta".to_string(), 1);
        let mut b = VectorClock::new();
        b.0.insert("alpha".to_string(), 1);
        b.0.insert("beta".to_string(), 2);
        assert_eq!(a.relation(&b), CausalRelation::Concurrent);
    }

    #[test]
    fn dominating_clock_is_happens_after() {
        // {alpha:2, beta:2} ≥ {alpha:1, beta:1} かつ少なくとも 1 つ > → HappensAfter。
        let mut a = VectorClock::new();
        a.0.insert("alpha".to_string(), 2);
        a.0.insert("beta".to_string(), 2);
        let mut b = VectorClock::new();
        b.0.insert("alpha".to_string(), 1);
        b.0.insert("beta".to_string(), 1);
        assert_eq!(a.relation(&b), CausalRelation::HappensAfter);
        assert_eq!(b.relation(&a), CausalRelation::HappensBefore);
    }

    #[test]
    fn merge_takes_componentwise_max_and_establishes_causality() {
        // マージ後の clock は両者を happens-after する（vector clock 標準手順）。
        let a = VectorClock::single("alpha", 2);
        let b = VectorClock::single("beta", 3);
        let mut merged = a.clone();
        merged.merge(&b);
        merged.increment("gamma"); // 受信側が自イベントを進める
        assert_eq!(merged.get("alpha"), 2);
        assert_eq!(merged.get("beta"), 3);
        assert_eq!(merged.get("gamma"), 1);
        assert_eq!(merged.relation(&a), CausalRelation::HappensAfter);
        assert_eq!(merged.relation(&b), CausalRelation::HappensAfter);
    }

    #[test]
    fn increment_is_deterministic_and_monotone() {
        let mut vc = VectorClock::new();
        vc.increment("alpha");
        vc.increment("alpha");
        vc.increment("beta");
        assert_eq!(vc.get("alpha"), 2);
        assert_eq!(vc.get("beta"), 1);
        // 未登録 source は 0。
        assert_eq!(vc.get("gamma"), 0);
    }

    #[test]
    fn relation_is_symmetric_for_concurrency() {
        // 全ペアで a.rel(b) と b.rel(a) が整合する（HappensBefore↔After・Concurrent↔Concurrent）。
        let samples = [
            VectorClock::single("alpha", 1),
            VectorClock::single("beta", 1),
            VectorClock::single("alpha", 5),
            VectorClock::new(),
        ];
        for a in &samples {
            for b in &samples {
                let ab = a.relation(b);
                let ba = b.relation(a);
                let consistent = match (ab, ba) {
                    (CausalRelation::HappensBefore, CausalRelation::HappensAfter)
                    | (CausalRelation::HappensAfter, CausalRelation::HappensBefore)
                    | (CausalRelation::Equal, CausalRelation::Equal)
                    | (CausalRelation::Concurrent, CausalRelation::Concurrent) => true,
                    _ => false,
                };
                assert!(consistent, "asymmetric: {ab:?} vs {ba:?}");
            }
        }
    }
}
