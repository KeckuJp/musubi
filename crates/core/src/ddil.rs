//! DdilProfile — DDIL 接続プロファイル → store-and-forward 挙動 ＋ MARK 写像。
//!
//! 断続・遅延・低帯域の接続状態を扱う **決定論テーブル**。各プロファイルを [`LinkState`] と
//! ORDER_UNKNOWN ポリシー・推奨 MARK へ一意に写す。
//!
//! 受入意味論:
//!
//! | プロファイル | 状態 | store-and-forward | MARK |
//! |---|---|---|---|
//! | NOMINAL | 常時接続・低遅延 | 即時転送 | OK/DEGRADED（品質由来） |
//! | DEGRADED | 帯域制限・高遅延 | 滞留キューで順次転送 | DEGRADED（遅延由来 reason_code） |
//! | DENIED | 全断 | キュー滞留（no-silent-drop）・復旧まで保持 | DEGRADED（回線断キュー品質） |
//! | INTERMITTENT | 断続接続 | 窓内にキューし窓が開いたら flush | DEGRADED |
//! | LIMITED | 低帯域・部分的 | 差分同期（delta-state CRDT）で最小転送 | DEGRADED |
//!
//! honest scope（over-claim 禁止）: 本 enum は **挙動の分類**であって tc/netem の数値仕様
//! （遅延 ms・損失率）ではない。実ネットワーク注入（tc/netem qdisc）は CI-sim（Linux）で行い、
//! 数値マッピングは Phase A sim の increment で詰める。敵対的 DoS/replay は Phase B red-team。

use crate::store_and_forward::LinkState;
use musubi_types::MarkStatus;

/// DDIL（Disconnected/Denied/Intermittent/Limited）の接続状態プロファイル。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DdilProfile {
    /// 完全接続。遅延/損失なし（即時転送）。
    Nominal,
    /// 接続あるが帯域/品質が低下（スロットリング/遅延増大）。
    Degraded,
    /// 完全遮断。store-and-forward モードに入る（全件滞留）。
    Denied,
    /// 断続的接続（短時間 Up が散発）。Up 窓に drain・Down 窓は Denied 扱い。
    Intermittent,
    /// 帯域極度制限（衛星 LoRa 相当）。差分同期で最小転送。
    Limited,
}

/// ORDER_UNKNOWN を付与するタイミングの方針。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderUnknownPolicy {
    /// Nominal では付与しない（通常フロー＝順序は保てる）。
    Never,
    /// 滞留後 drain するとき、`Concurrent` な envelope にのみ付与（store-and-forward の既定）。
    OnDrainConcurrentOnly,
    /// Intermittent では Up/Down 境界をまたぐ全 buffered に付与
    /// （Up 窓が短すぎて vector clock の継続性が失われ因果確定不能）。
    OnDrainAllBuffered,
}

/// DDIL プロファイルから [`LinkState`]・ORDER_UNKNOWN ポリシー・推奨 MARK への写像。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DdilBehavior {
    /// 現在の link state（store-and-forward のゲート）。
    pub link_state: LinkState,
    /// この状態での ORDER_UNKNOWN 付与ポリシー。
    pub order_unknown_policy: OrderUnknownPolicy,
    /// MARK への推奨変更（`None` = 現行 MARK＝`derive_mark` の結果のまま）。
    pub mark_effect: Option<MarkStatus>,
    /// `mark_effect` を出すときの reason_code 含意（`mark_effect=None` のときは空 `&str`）。
    pub mark_reason: &'static str,
}

/// DDIL プロファイル → 挙動マップ（決定論テーブル）。
///
/// 写像根拠:
/// - `Denied` = `Down` + `OnDrainConcurrentOnly`: 回線断後の drain では、断絶前後でも同一ノードの
///   逐次カウントは `HappensBefore` で確定するため、因果不明（`Concurrent`）なものだけ ORDER_UNKNOWN。
/// - `Intermittent` = `Down`(基底) + `OnDrainAllBuffered`: Up 窓が短いと前後の窓の buffered を
///   まとめて drain するとき境界前後の全件で vector clock 継続性が失われる → 全件 ORDER_UNKNOWN。
/// - `Degraded`/`Limited` = `Up` + `DEGRADED` 推奨: 接続は継続するが品質低下を `"link-degraded"`/
///   `"link-limited"` の reason_code で MARK に追記推奨（順序は `Up` のまま保てる）。
#[must_use]
pub fn ddil_behavior(profile: DdilProfile) -> DdilBehavior {
    match profile {
        DdilProfile::Nominal => DdilBehavior {
            link_state: LinkState::Up,
            order_unknown_policy: OrderUnknownPolicy::Never,
            mark_effect: None, // MARK は derive_mark のまま。
            mark_reason: "",
        },
        DdilProfile::Degraded => DdilBehavior {
            link_state: LinkState::Up,                       // 接続はある（遅延↑）。
            order_unknown_policy: OrderUnknownPolicy::Never, // 順序は保てる。
            mark_effect: Some(MarkStatus::Degraded),
            mark_reason: "link-degraded",
        },
        DdilProfile::Denied => DdilBehavior {
            link_state: LinkState::Down, // 全件バッファ。
            order_unknown_policy: OrderUnknownPolicy::OnDrainConcurrentOnly,
            mark_effect: Some(MarkStatus::Degraded), // 回線断期間のキュー品質。
            mark_reason: "link-denied-queued",
        },
        DdilProfile::Intermittent => DdilBehavior {
            link_state: LinkState::Down, // 基底は Down（Up 窓は外部イベントで set_link(Up)）。
            order_unknown_policy: OrderUnknownPolicy::OnDrainAllBuffered,
            mark_effect: Some(MarkStatus::Degraded),
            mark_reason: "link-intermittent",
        },
        DdilProfile::Limited => DdilBehavior {
            link_state: LinkState::Up, // 接続自体はある。
            order_unknown_policy: OrderUnknownPolicy::Never,
            mark_effect: Some(MarkStatus::Degraded),
            mark_reason: "link-limited",
        },
    }
}

/// drain 後の 1 件について、プロファイルのポリシーと per-item の並行フラグから
/// **ORDER_UNKNOWN を最終的に発火させるか**を決める（決定論）。
///
/// - [`OrderUnknownPolicy::Never`] → 常に `false`。
/// - [`OrderUnknownPolicy::OnDrainConcurrentOnly`] → `item_concurrent` のとき `true`
///   （store-and-forward の `DrainedItem.order_unknown` をそのまま採る）。
/// - [`OrderUnknownPolicy::OnDrainAllBuffered`] → drain された全件に `true`
///   （Intermittent の Up 窓境界で因果継続性が失われるため）。
///
/// `MarkStatus::OrderUnknown`（予約値・tag=0x03・`derive_mark` は決して返さない）の
/// 唯一の発火元がこの判定である。呼び出し側（fan-out 層）が `true` の item を MARK レジャへ
/// `OrderUnknown` で計上する（envelope 本体は不変・no-silent-drop）。
#[must_use]
pub fn order_unknown_fires(policy: OrderUnknownPolicy, item_concurrent: bool) -> bool {
    match policy {
        OrderUnknownPolicy::Never => false,
        OrderUnknownPolicy::OnDrainConcurrentOnly => item_concurrent,
        OrderUnknownPolicy::OnDrainAllBuffered => true,
    }
}

#[cfg(test)]
mod tests {
    //! DDIL プロファイル 5 値 → LinkState/ORDER_UNKNOWN ポリシー/MARK の一意写像、
    //! および profile→MARK 反映の決定論検証。

    use super::{DdilProfile, OrderUnknownPolicy, ddil_behavior, order_unknown_fires};
    use crate::store_and_forward::LinkState;
    use musubi_types::MarkStatus;

    #[test]
    fn five_profiles_map_to_link_state_uniquely() {
        // must_prove ④: 5 プロファイルが LinkState へ一意に写る。
        assert_eq!(
            ddil_behavior(DdilProfile::Nominal).link_state,
            LinkState::Up
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Degraded).link_state,
            LinkState::Up
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Denied).link_state,
            LinkState::Down
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Intermittent).link_state,
            LinkState::Down
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Limited).link_state,
            LinkState::Up
        );
    }

    #[test]
    fn degraded_profiles_recommend_degraded_mark_with_nonempty_reason() {
        // must_prove ③: 各 DDIL 劣化プロファイルが DEGRADED MARK＋非空 reason_code を推奨する。
        for p in [
            DdilProfile::Degraded,
            DdilProfile::Denied,
            DdilProfile::Intermittent,
            DdilProfile::Limited,
        ] {
            let b = ddil_behavior(p);
            assert_eq!(
                b.mark_effect,
                Some(MarkStatus::Degraded),
                "{p:?} must recommend DEGRADED"
            );
            assert!(!b.mark_reason.is_empty(), "{p:?} reason must be non-empty");
        }
        // Nominal は MARK を変えない（derive_mark のまま）。
        let nom = ddil_behavior(DdilProfile::Nominal);
        assert_eq!(nom.mark_effect, None);
        assert!(nom.mark_reason.is_empty());
    }

    #[test]
    fn order_unknown_policy_matches_profile_semantics() {
        // Nominal/Degraded/Limited は Never（順序保てる）、Denied は Concurrent のみ、
        // Intermittent は全 buffered。
        assert_eq!(
            ddil_behavior(DdilProfile::Nominal).order_unknown_policy,
            OrderUnknownPolicy::Never
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Degraded).order_unknown_policy,
            OrderUnknownPolicy::Never
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Limited).order_unknown_policy,
            OrderUnknownPolicy::Never
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Denied).order_unknown_policy,
            OrderUnknownPolicy::OnDrainConcurrentOnly
        );
        assert_eq!(
            ddil_behavior(DdilProfile::Intermittent).order_unknown_policy,
            OrderUnknownPolicy::OnDrainAllBuffered
        );
    }

    #[test]
    fn order_unknown_fires_respects_policy() {
        // Never: 並行でも発火しない。
        assert!(!order_unknown_fires(OrderUnknownPolicy::Never, true));
        assert!(!order_unknown_fires(OrderUnknownPolicy::Never, false));
        // OnDrainConcurrentOnly: per-item の並行フラグに従う。
        assert!(order_unknown_fires(
            OrderUnknownPolicy::OnDrainConcurrentOnly,
            true
        ));
        assert!(!order_unknown_fires(
            OrderUnknownPolicy::OnDrainConcurrentOnly,
            false
        ));
        // OnDrainAllBuffered: 並行でなくても発火（Up 窓境界の継続性喪失）。
        assert!(order_unknown_fires(
            OrderUnknownPolicy::OnDrainAllBuffered,
            false
        ));
        assert!(order_unknown_fires(
            OrderUnknownPolicy::OnDrainAllBuffered,
            true
        ));
    }
}
