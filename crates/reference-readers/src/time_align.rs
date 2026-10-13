use musubi_reference_types::{
    ClockBasis, ClockRateClass, ClockRateConfig, ClockRateGap, ClockRateOutcome, ClockRateReport,
    OffsetEstimate, OffsetSource, OrderRelation, TimeAlignment, order_with_bounds,
};

use crate::{Observation, default_time_confidence};

/// drift 上限（ppm・recorded-input contract [X]「MCU 発振器 ppm は board 依存」＝保守的な仮置き 100 ppm）。
pub const DRIFT_PPM_BOUND: f64 = 100.0;
/// offset 不連続の閾値（µs・PX4 `≥5 s` 再設定／reboot を拾う・1 s）。
pub const DISCONTINUITY_US: i64 = 1_000_000;
/// anchor が log 末尾からこれ以上離れていれば「途中で止まった」（ms）。
pub const ANCHOR_TAIL_GAP_MS: i64 = 5_000;

/// 1 記録（1 file）の観測列から整列を推定する。
#[must_use]
pub fn estimate_alignment(obs: &[Observation], default_basis: ClockBasis) -> TimeAlignment {
    let mut anchors: Vec<(u64, i64)> = obs
        .iter()
        .filter_map(|o| Some((o.t_boot_us?, o.anchor_unix_us?)))
        .collect();
    let unrepresentable = || TimeAlignment {
        basis: if default_basis == ClockBasis::HostReceived {
            ClockBasis::HostReceived
        } else {
            ClockBasis::GpsSuspect
        },
        offset: None,
    };
    anchors.sort_unstable();
    anchors.dedup();
    if matches!(
        default_basis,
        ClockBasis::HostReceived | ClockBasis::Unknown
    ) && anchors.is_empty()
    {
        return TimeAlignment {
            basis: default_basis,
            offset: None,
        };
    }
    if anchors.is_empty() {
        return TimeAlignment {
            basis: ClockBasis::BootRelative,
            offset: None,
        };
    }
    let mut offsets: Vec<i128> = anchors
        .iter()
        .map(|&(b, a)| i128::from(a) - i128::from(b))
        .collect();
    let discontinuity = offsets
        .windows(2)
        .any(|w| (w[1] - w[0]).abs() >= i128::from(DISCONTINUITY_US));
    offsets.sort_unstable();
    let median = offsets[offsets.len() / 2];
    let spread = offsets
        .iter()
        .map(|o| (o - median).abs())
        .max()
        .unwrap_or(0);
    let (first_boot, last_boot) = (anchors[0].0, anchors[anchors.len() - 1].0);
    let log_last_boot = obs
        .iter()
        .filter_map(|o| o.t_boot_us)
        .max()
        .unwrap_or(last_boot);
    // 最後の anchor から log 末尾までの外挿区間に drift 上限を乗せる（anchor 1 点なら全区間）。
    let extrapolated_us = log_last_boot.saturating_sub(last_boot) as f64
        + if anchors.len() == 1 {
            (log_last_boot - first_boot) as f64
        } else {
            0.0
        };
    let bound = spread + (extrapolated_us * DRIFT_PPM_BOUND * 1e-6).ceil() as i128;
    let (Ok(median), Ok(bound)) = (i64::try_from(median), i64::try_from(bound)) else {
        return unrepresentable();
    };
    let tail_gap_ms = (log_last_boot.saturating_sub(last_boot) / 1000) as i64;
    // anchor の途切れ（fix 喪失区間）: 末尾だけでなく途中の穴も「anchor 断続」＝log 全体としては推定 offset 扱い
    // （区間ごとの basis 切替は recorded-input contract 画面側・本 module は 1 log 1 basis で保守的に落とす）。
    let max_inner_gap_ms = anchors
        .windows(2)
        .map(|p| ((p[1].0 - p[0].0) / 1000) as i64)
        .max()
        .unwrap_or(0);
    let basis = if matches!(default_basis, ClockBasis::HostReceived) {
        ClockBasis::HostReceived // host 軸は据え置き（機体 offset は付加情報・不連続は `discontinuity` flag に残る）。
    } else if discontinuity {
        ClockBasis::GpsSuspect
    } else if tail_gap_ms <= ANCHOR_TAIL_GAP_MS && max_inner_gap_ms <= ANCHOR_TAIL_GAP_MS {
        ClockBasis::GpsLocked
    } else {
        ClockBasis::BootRelativeOffsetEstimated
    };
    TimeAlignment {
        basis,
        offset: Some(OffsetEstimate {
            offset_us: median,
            bound_us: bound.max(1),
            source: OffsetSource::InLogAnchor,
            anchors: anchors.len() as u32,
            discontinuity,
        }),
    }
}

/// 記録済み anchor 対から **相対 clock rate** を見積もる（recorded-input contract・任意＝設定を宣言した記録だけ）。
///
/// `DRIFT_PPM_BOUND` は「仮置きの上限」という**仮定**のままで、これはその仮定とは別に、保存済みの
/// (記録側 device 時計, 宣言 reference 時計) の対が実際にどれだけ離れていったかの**観測区間**を出す。
/// 片方を犯人にしない＝これは 2 つの読みの**相対**変化であって、どちらの時計がずれたかは言わない。
///
/// **最初と最後の対だけで決まる区間平均**である。途中の各区間が安定していたことは示さない＝同じ平均は
/// 「ずっと一定」でも「速い区間と遅い区間の打ち消し」でも出る。中間の anchor は `discontinuity` 判定と
/// 巻き戻し判定には効くが、率そのものは両端で決まる。瞬時安定度（Allan 偏差等）はここには無い。
///
/// `anchor_uncertainty_us` は呼び手が渡す一般の値で、schema ごとの量子化 floor は profile 読み込み側
/// （`profile::CLOCK_RATE_FORMATS`）の責務＝ここの算術はどの幅でも同じに成り立つ。
///
/// 式（[`ClockRateConfig::anchor_uncertainty_us`] を `u`・最初と最後の anchor 対を `(b₀,a₀)`・`(bₙ,aₙ)` として）:
///
/// ```text
/// Δb = bₙ − b₀                 記録側 device 時計の経過（µs・分母）
/// Δa = aₙ − a₀                 宣言 reference の経過（µs）
/// r_ppm = (Δa − Δb) / Δb × 10⁶
/// ```
///
/// `r_ppm > 0`＝宣言 reference のほうが多く進んだ。区間は分子 `±2u`・分母 `±2u` の 4 隅を取り、低い端は
/// floor・高い端は ceil＝**外側**へ丸めるので、持っている以上の精度を主張しない。演算は `i128`・float
/// 不使用で、各 offset `aᵢ − bᵢ` を差より**先**に作るので 1.7×10¹⁸ µs の epoch は最初の引き算で相殺し、
/// 掛け算には届かない。
///
/// 基礎が足りなければ [`ClockRateOutcome::InsufficientBasis`] に理由を名指しする（推測も代入もしない）。
/// 区間が閾値をまたぐときは [`ClockRateClass::Indeterminate`]＝**PASS ではない**。
#[must_use]
pub fn estimate_relative_rate(
    obs: &[Observation],
    al: &TimeAlignment,
    cfg: &ClockRateConfig,
) -> ClockRateReport {
    let gap = |g: ClockRateGap| ClockRateReport {
        outcome: ClockRateOutcome::InsufficientBasis(g),
        anchors: 0,
        span_us: 0,
        config: *cfg,
    };
    if matches!(al.basis, ClockBasis::HostReceived | ClockBasis::Unknown) {
        return gap(ClockRateGap::ClockBasisNotDeviceClock);
    }
    match al.offset {
        None => return gap(ClockRateGap::FewerThanTwoAnchors),
        Some(o) if o.source != OffsetSource::InLogAnchor => {
            return gap(ClockRateGap::OffsetSourceNotInLogAnchor);
        }
        // 既存の不連続判定（GNSS 時刻跳び／reboot）をそのまま再利用する。跨いで率を出さない。
        Some(o) if o.discontinuity => return gap(ClockRateGap::OffsetDiscontinuity),
        Some(_) => {}
    }
    // 記録順のまま見る。`estimate_alignment` は sort するので device 時計の巻き戻し（reset）が消えており、
    // ここで初めて見える。
    let mut sorted: Vec<(u64, i64)> = obs
        .iter()
        .filter_map(|o| Some((o.t_boot_us?, o.anchor_unix_us?)))
        .collect();
    if sorted.windows(2).any(|w| w[1].0 < w[0].0) {
        return gap(ClockRateGap::DeviceClockNotMonotonic);
    }
    sorted.sort_unstable();
    sorted.dedup();
    let (Some(&(b0, a0)), Some(&(bn, an))) = (sorted.first(), sorted.last()) else {
        return gap(ClockRateGap::FewerThanTwoAnchors);
    };
    if sorted.len() < 2 || bn == b0 {
        return gap(ClockRateGap::FewerThanTwoAnchors);
    }
    let span = i128::from(bn) - i128::from(b0);
    if span < i128::from(cfg.min_span_us) {
        return gap(ClockRateGap::SpanShorterThanDeclaredMinimum);
    }
    let two_u = 2 * i128::from(cfg.anchor_uncertainty_us);
    if span - two_u <= 0 {
        return gap(ClockRateGap::UncertaintyNotSmallerThanSpan);
    }
    // offset を先に作る＝epoch はここで相殺する。
    let d_offset = (i128::from(an) - i128::from(bn)) - (i128::from(a0) - i128::from(b0));
    let mut low = i128::MAX;
    let mut high = i128::MIN;
    for n in [d_offset - two_u, d_offset + two_u] {
        for d in [span - two_u, span + two_u] {
            let scaled = n * 1_000_000;
            low = low.min(scaled.div_euclid(d));
            high = high.max(-((-scaled).div_euclid(d)));
        }
    }
    let t = i128::from(cfg.max_abs_rate_ppm);
    let class = if low >= -t && high <= t {
        ClockRateClass::WithinDeclaredLimit
    } else if low > t || high < -t {
        ClockRateClass::OutsideDeclaredLimit
    } else {
        // 区間が閾値に重なる＝この記録では決まらない。PASS へ丸めない。
        ClockRateClass::Indeterminate
    };
    // 報告値に収まらない入力は**丸めずに**基礎不足として返す。clamp すれば偽の有限区間に、`as` で
    // 落とせば負の経過時間になり、どちらも「読める答え」の顔をしてしまう。
    let (Ok(span_us), Ok(low_ppm), Ok(high_ppm)) =
        (i64::try_from(span), i64::try_from(low), i64::try_from(high))
    else {
        return gap(ClockRateGap::OutOfRepresentableRange);
    };
    ClockRateReport {
        outcome: ClockRateOutcome::Observed {
            low_ppm,
            high_ppm,
            class,
        },
        anchors: sorted.len() as u32,
        span_us,
        config: *cfg,
    }
}

/// anchor の無い boot 相対 log を、同一 asset の別 source（handset RTC 等）の**開始時刻**に合わせる粗い整列（recorded-input contract 手法 C の
/// 最小形・「記録開始 ≈ 同時」という仮定 [I]）。誤差帯は `bound_us`（既定 10 s）で広く取り、basis は
/// `boot_relative_offset_estimated`（source `CrossLogEvent`）。in-log anchor がある log には使わない（`estimate_alignment` 優先）。
/// 欠落そのものを整列に使わない（循環回避・recorded-input contract 手法 C の注意）。
#[must_use]
pub fn align_by_start(
    obs: &[Observation],
    reference_start_wall_ms: i64,
    bound_us: i64,
) -> Option<TimeAlignment> {
    let first_boot = obs.iter().filter_map(|o| o.t_boot_us).min()?;
    let offset_us =
        i64::try_from(i128::from(reference_start_wall_ms) * 1000 - i128::from(first_boot)).ok()?;
    Some(TimeAlignment {
        basis: ClockBasis::BootRelativeOffsetEstimated,
        offset: Some(OffsetEstimate {
            offset_us,
            bound_us,
            source: OffsetSource::CrossLogEvent,
            anchors: 1,
            discontinuity: false,
        }),
    })
}

/// boot 相対 µs → (wall µs, ±bound µs)。offset 無しなら `None`。
#[must_use]
pub fn to_wall_us(al: &TimeAlignment, t_boot_us: u64) -> Option<(i64, i64)> {
    let o = al.offset?;
    Some((
        i64::try_from(i128::from(t_boot_us) + i128::from(o.offset_us)).ok()?,
        o.bound_us,
    ))
}

/// 観測列へ整列を反映する（昇格／降格＝`clock_basis`・`time_confidence`・`wall_ms`）。host 軸は `wall_ms=t_ms` のまま。
#[must_use]
pub fn promote(obs: &[Observation], al: &TimeAlignment) -> Vec<Observation> {
    let mut out = obs.to_vec();
    for o in &mut out {
        if matches!(
            o.clock_basis,
            ClockBasis::HostReceived | ClockBasis::Unknown
        ) {
            continue;
        }
        o.clock_basis = al.basis;
        o.time_confidence = default_time_confidence(al.basis);
        o.wall_ms = o
            .t_boot_us
            .and_then(|b| to_wall_us(al, b))
            .map(|(w, _)| w / 1000);
    }
    out
}

/// 2 時点（wall µs＋誤差帯）の前後判定。
#[must_use]
pub const fn order(a_us: i64, a_bound: i64, b_us: i64, b_bound: i64) -> OrderRelation {
    order_with_bounds(a_us, a_bound, b_us, b_bound)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ChannelId, FieldValue, SourceRole, observation_digest};

    fn ob(t_boot_us: u64, anchor: Option<i64>, basis: ClockBasis) -> Observation {
        let fields = vec![("x".to_string(), FieldValue::I64(t_boot_us as i64))];
        Observation {
            t_ms: (t_boot_us / 1000) as i64,
            clock_basis: basis,
            time_confidence: 0.2,
            source_role: SourceRole::Fc,
            channel: ChannelId::GpsEkf,
            digest: observation_digest(0, ChannelId::GpsEkf, &fields),
            fields,
            stale: false,
            t_boot_us: Some(t_boot_us),
            anchor_unix_us: anchor,
            wall_ms: None,
        }
    }

    /// `n` 対の anchor を作る。device 時計は `step_us` 刻み、reference は同じ刻みに `ppm` 分だけ多く（少なく）進む。
    fn paired(n: u64, step_us: u64, b0: u64, a0: i64, ppm: i64) -> Vec<Observation> {
        (0..n)
            .map(|i| {
                let elapsed = (i * step_us) as i64;
                ob(
                    b0 + i * step_us,
                    Some(a0 + elapsed + elapsed * ppm / 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect()
    }

    fn cfg(uncertainty_us: i64, min_span_us: i64, max_abs_rate_ppm: i64) -> ClockRateConfig {
        ClockRateConfig {
            anchor_uncertainty_us: uncertainty_us,
            min_span_us,
            max_abs_rate_ppm,
        }
    }

    fn rate_of(obs: &[Observation], c: &ClockRateConfig) -> ClockRateReport {
        let al = estimate_alignment(obs, ClockBasis::BootRelative);
        estimate_relative_rate(obs, &al, c)
    }

    fn observed(obs: &[Observation], c: &ClockRateConfig) -> (i64, i64, ClockRateClass) {
        match rate_of(obs, c).outcome {
            ClockRateOutcome::Observed {
                low_ppm,
                high_ppm,
                class,
            } => (low_ppm, high_ppm, class),
            other => panic!("expected an observed interval, got {other:?}"),
        }
    }

    /// 独立した 2 本（+50 ppm／−50 ppm）。符号の基準・区間の外側丸め・閾値ちょうどの扱いを 1 か所で押さえる。
    #[test]
    fn observed_relative_rate_is_a_signed_outward_rounded_interval_classified_against_the_declared_limit()
     {
        // 100 s・1 s 刻み・reference が 5 000 µs 多く進む＝+50 ppm。宣言 pairing ±100 µs。
        let faster_reference = paired(101, 1_000_000, 37_000_000, 1_000_000_000_000_000, 50);
        let (low, high, _) = observed(&faster_reference, &cfg(100, 60_000_000, 1_000));
        // 分子 ±200 µs・分母 ±200 µs の 4 隅を外側へ丸めた閉区間。真の 50 ppm を必ず含む。
        assert_eq!((low, high), (47, 53));
        // 逆符号は鏡像＝「どちらの時計がずれたか」ではなく相対変化の向きだけを言う。
        let slower_reference = paired(101, 1_000_000, 37_000_000, 1_000_000_000_000_000, -50);
        assert_eq!(
            observed(&slower_reference, &cfg(100, 60_000_000, 1_000)).0
                ..=observed(&slower_reference, &cfg(100, 60_000_000, 1_000)).1,
            -53..=-47
        );

        let class = |obs: &[Observation], t: i64| observed(obs, &cfg(100, 60_000_000, t)).2;
        // 閾値ちょうど: 上端 53 == T は範囲内、1 ppm 下げると区間が閾値をまたぐ＝PASS ではない。
        assert_eq!(
            class(&faster_reference, 53),
            ClockRateClass::WithinDeclaredLimit
        );
        assert_eq!(class(&faster_reference, 52), ClockRateClass::Indeterminate);
        assert_eq!(
            class(&slower_reference, 53),
            ClockRateClass::WithinDeclaredLimit
        );
        assert_eq!(class(&slower_reference, 52), ClockRateClass::Indeterminate);
        // 下端ちょうど: 47 は T=47 を超えないので未決、T=46 で初めて全区間が外。
        assert_eq!(class(&faster_reference, 47), ClockRateClass::Indeterminate);
        assert_eq!(
            class(&faster_reference, 46),
            ClockRateClass::OutsideDeclaredLimit
        );
        assert_eq!(
            class(&slower_reference, 46),
            ClockRateClass::OutsideDeclaredLimit
        );
        // 宣言した幅そのものが結論と一緒に残る（何に対する判定かを後から取り違えない）。
        let r = rate_of(&faster_reference, &cfg(100, 60_000_000, 53));
        assert_eq!(r.anchors, 101);
        assert_eq!(r.span_us, 100_000_000);
        assert_eq!(r.config.max_abs_rate_ppm, 53);
    }

    /// 1.7×10¹⁸ µs の epoch でも同じ区間＝offset を差より先に作るので桁落ちも overflow も起きない。
    /// 併せて 10 000 対（normal capacity）を 1 記録で通す。
    #[test]
    fn large_epoch_and_ten_thousand_pairs_give_the_same_interval_as_a_small_epoch() {
        let c = cfg(100, 60_000_000, 1_000);
        let near_zero = observed(&paired(101, 1_000_000, 0, 1_000_000, 50), &c);
        let year_2026 = observed(
            &paired(101, 1_000_000, 37_000_000, 1_788_166_800_000_000, 50),
            &c,
        );
        assert_eq!(near_zero, year_2026);

        // 10 000 対・10 ms 刻み＝9 999 区間で 99.99 s。区間は両端の対だけで決まり、件数では変わらない
        // （50 ppm の drift は整数で 4 999 µs に切れるので上端が 1 ppm 分だけ狭い）。
        let many = paired(10_000, 10_000, 37_000_000, 1_788_166_800_000_000, 50);
        let r = rate_of(&many, &c);
        assert_eq!(r.anchors, 10_000);
        assert_eq!(r.span_us, 99_990_000);
        assert_eq!(
            r.outcome,
            ClockRateOutcome::Observed {
                low_ppm: 47,
                high_ppm: 52,
                class: ClockRateClass::WithinDeclaredLimit,
            }
        );
    }

    /// 基礎が足りないときは理由を名指しして率を出さない（推測も 0 の代入もしない）。
    #[test]
    fn insufficient_basis_is_named_and_never_substituted_by_a_rate() {
        let gap = |obs: &[Observation], c: &ClockRateConfig| match rate_of(obs, c).outcome {
            ClockRateOutcome::InsufficientBasis(g) => g,
            other => panic!("expected a named gap, got {other:?}"),
        };
        let c = cfg(100, 60_000_000, 50);

        let none: Vec<Observation> = (0..10)
            .map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        assert_eq!(gap(&none, &c), ClockRateGap::FewerThanTwoAnchors);
        assert_eq!(
            gap(&paired(1, 1_000_000, 0, 1_000_000_000_000_000, 50), &c),
            ClockRateGap::FewerThanTwoAnchors
        );
        // anchor が近すぎる: 2 s しか離れていないのに 60 s を宣言している。
        assert_eq!(
            gap(&paired(3, 1_000_000, 0, 1_000_000_000_000_000, 50), &c),
            ClockRateGap::SpanShorterThanDeclaredMinimum
        );
        // 宣言した pairing 幅が区間の半分以上＝率は有界にならない。
        assert_eq!(
            gap(
                &paired(2, 1_000, 0, 1_000_000_000_000_000, 0),
                &cfg(500, 1_000, 50)
            ),
            ClockRateGap::UncertaintyNotSmallerThanSpan
        );
        // 報告幅に収まらない入力は偽の有限区間にも負の経過時間にもならず、名前のついた拒否になる。
        // `estimate_alignment` 自身の i64 算術がこの手の値では先に音を上げるので、検査対象の関数へ
        // 直接 alignment を渡して変換 guard だけを切り出す。
        let clean = TimeAlignment {
            basis: ClockBasis::BootRelative,
            offset: Some(OffsetEstimate {
                offset_us: 0,
                bound_us: 1,
                source: OffsetSource::InLogAnchor,
                anchors: 2,
                discontinuity: false,
            }),
        };
        let direct = |obs: &[Observation], c: &ClockRateConfig| {
            estimate_relative_rate(obs, &clean, c).outcome
        };
        // span が i64 を超える（device 時計は u64）。clamp／`as` なら**負の経過時間**になっていた。
        let huge_span = [
            ob(0, Some(0), ClockBasis::BootRelative),
            ob(u64::MAX, Some(0), ClockBasis::BootRelative),
        ];
        assert_eq!(
            direct(&huge_span, &cfg(1_000, 1, 50)),
            ClockRateOutcome::InsufficientBasis(ClockRateGap::OutOfRepresentableRange)
        );
        // 端点が i64 を超える（区間が極端に短く offset 差が極端に大きい）。clamp なら
        // i64::MAX という**偽の有限区間**が「読める答え」として出ていた。
        let huge_endpoint = [
            ob(0, Some(0), ClockBasis::BootRelative),
            ob(3, Some(30_000_000_000_003), ClockBasis::BootRelative),
        ];
        assert_eq!(
            direct(&huge_endpoint, &cfg(1, 1, 50)),
            ClockRateOutcome::InsufficientBasis(ClockRateGap::OutOfRepresentableRange)
        );
        // すぐ内側の値は通常どおり有限区間として出る（guard が広すぎない）。
        let representable = [
            ob(0, Some(0), ClockBasis::BootRelative),
            ob(3, Some(3_000_000_003), ClockBasis::BootRelative),
        ];
        assert!(matches!(
            direct(&representable, &cfg(1, 1, 50)),
            ClockRateOutcome::Observed { .. }
        ));
        // 極端な宣言値でも桁あふれで落ちず、基礎不足として返す（`i64` 幅なら 2×i64::MAX で溢れる）。
        let full_span = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 50);
        assert_eq!(
            gap(&full_span, &cfg(i64::MAX, 1, 50)),
            ClockRateGap::UncertaintyNotSmallerThanSpan
        );
        assert_eq!(
            gap(&full_span, &cfg(100, i64::MAX, 50)),
            ClockRateGap::SpanShorterThanDeclaredMinimum
        );
        // 閾値が極端でも分類は成立する（区間は変わらない）。
        assert_eq!(
            observed(&full_span, &cfg(100, 60_000_000, i64::MAX)),
            (47, 53, ClockRateClass::WithinDeclaredLimit)
        );
        // 既存の不連続判定（GNSS 時刻跳び／reboot）を跨いで率を出さない。
        let mut jump = paired(11, 1_000_000, 0, 1_000_000_000_000_000, 0);
        jump.extend((11..22).map(|i| {
            ob(
                i * 1_000_000,
                Some(1_000_000_000_000_000 + i as i64 * 1_000_000 + 7_000_000),
                ClockBasis::BootRelative,
            )
        }));
        assert_eq!(gap(&jump, &c), ClockRateGap::OffsetDiscontinuity);
        // device 時計の巻き戻し。offset は一貫しているので既存の不連続判定には映らず、
        // `estimate_alignment` は sort するので消える。記録順で見て初めて分かる。
        let mut reset = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 0);
        reset.push(ob(
            50_500_000,
            Some(1_000_000_000_000_000 + 50_500_000),
            ClockBasis::BootRelative,
        ));
        let al = estimate_alignment(&reset, ClockBasis::BootRelative);
        assert!(
            !al.offset.expect("offset").discontinuity,
            "the existing flag does not see this reset"
        );
        assert_eq!(
            estimate_relative_rate(&reset, &al, &c).outcome,
            ClockRateOutcome::InsufficientBasis(ClockRateGap::DeviceClockNotMonotonic)
        );
        // 開始時刻を合わせただけの粗い整列（±10 s の仮定）は率の基礎にならない。
        let cross = align_by_start(&none, 1_788_166_800_000, 10_000_000).expect("aligned");
        assert_eq!(
            estimate_relative_rate(&none, &cross, &c).outcome,
            ClockRateOutcome::InsufficientBasis(ClockRateGap::OffsetSourceNotInLogAnchor)
        );
        // host 軸（受領時刻）は機体の時計ではない。
        let host = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 50);
        let host_al = estimate_alignment(&host, ClockBasis::HostReceived);
        assert_eq!(
            estimate_relative_rate(&host, &host_al, &c).outcome,
            ClockRateOutcome::InsufficientBasis(ClockRateGap::ClockBasisNotDeviceClock)
        );
    }

    /// 仮置き 100 ppm の意味は変わらない: 仮定は仮定のまま、観測区間はそれを置き換えも狭めもしない。
    #[test]
    fn the_assumed_drift_bound_keeps_its_meaning_and_the_observed_interval_does_not_touch_it() {
        assert!((DRIFT_PPM_BOUND - 100.0).abs() < f64::EPSILON);
        let obs = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 50);
        let before = estimate_alignment(&obs, ClockBasis::BootRelative);
        let _ = estimate_relative_rate(&obs, &before, &cfg(100, 60_000_000, 1_000));
        let after = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(before, after);
        // anchor が末尾まで続くので外挿は無く、bound は anchor の散らばりのみ＝観測率は混ざらない
        // （offset は 5 000 µs 分だけ直線的に動き、中央値からの最大乖離はその半分）。
        assert_eq!(after.basis, ClockBasis::GpsLocked);
        assert_eq!(after.offset.expect("offset").bound_us, 2_500);
    }

    #[test]
    fn anchors_to_the_end_give_gps_locked_and_offset() {
        let obs: Vec<Observation> = (0..100)
            .map(|i| {
                ob(
                    37_000_000 + i * 1_000_000,
                    Some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(al.basis, ClockBasis::GpsLocked);
        let o = al.offset.expect("offset");
        assert_eq!(o.offset_us, 1_000_000_000_000_000 - 37_000_000);
        assert!(!o.discontinuity);
        let p = promote(&obs, &al);
        assert_eq!(p[0].wall_ms, Some(1_000_000_000_000));
        assert_eq!(p[0].clock_basis, ClockBasis::GpsLocked);
    }

    #[test]
    fn anchors_stopping_midway_give_offset_estimated_with_wider_bound() {
        let mut obs: Vec<Observation> = (0..50)
            .map(|i| {
                ob(
                    i * 1_000_000,
                    Some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        obs.extend((50..100).map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative)));
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(al.basis, ClockBasis::BootRelativeOffsetEstimated);
        // 50 s 外挿 × 100 ppm = 5 ms。
        assert_eq!(al.offset.expect("offset").bound_us, 5_000);
    }

    #[test]
    fn align_by_start_gives_cross_log_offset_with_wide_bound_and_none_without_boot_time() {
        let obs: Vec<Observation> = (0..10)
            .map(|i| ob(8_000_000 + i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        let al = align_by_start(&obs, 1_788_166_800_000, 10_000_000).expect("aligned");
        assert_eq!(al.basis, ClockBasis::BootRelativeOffsetEstimated);
        let o = al.offset.expect("offset");
        assert_eq!(o.source, OffsetSource::CrossLogEvent);
        assert_eq!(o.offset_us, 1_788_166_800_000_000 - 8_000_000);
        assert_eq!(o.bound_us, 10_000_000);
        assert_eq!(
            to_wall_us(&al, 8_000_000),
            Some((1_788_166_800_000_000, 10_000_000))
        );
        let mut no_boot = ob(0, None, ClockBasis::BootRelative);
        no_boot.t_boot_us = None;
        assert!(align_by_start(&[no_boot], 0, 1).is_none());
    }

    #[test]
    fn single_anchor_bound_covers_whole_log_and_inner_gap_demotes_to_estimated() {
        let mut obs: Vec<Observation> = (0..100)
            .map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        obs[0].anchor_unix_us = Some(1_000_000_000_000_000);
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        // 1 anchor: 99 s 全区間 × 100 ppm = 9.9 ms（末尾外挿 99 s＋全区間 99 s で 19.8 ms）。
        assert_eq!(al.offset.expect("o").bound_us, 19_800);
        assert_eq!(al.basis, ClockBasis::BootRelativeOffsetEstimated);
        // anchor が 0..30 s と 60..99 s（30 s の穴）→ 末尾まで anchor があっても推定扱い。
        let obs: Vec<Observation> = (0..100)
            .map(|i| {
                let anchored = i < 30 || i >= 60;
                ob(
                    i * 1_000_000,
                    anchored.then_some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        assert_eq!(
            estimate_alignment(&obs, ClockBasis::BootRelative).basis,
            ClockBasis::BootRelativeOffsetEstimated
        );
    }

    #[test]
    fn no_anchor_is_boot_relative_and_jump_is_gps_suspect_and_order_unknown_when_bands_overlap() {
        let obs: Vec<Observation> = (0..10)
            .map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        assert_eq!(
            estimate_alignment(&obs, ClockBasis::BootRelative).basis,
            ClockBasis::BootRelative
        );
        let mut obs: Vec<Observation> = (0..10)
            .map(|i| {
                ob(
                    i * 1_000_000,
                    Some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        obs.extend((10..20).map(|i| {
            ob(
                i * 1_000_000,
                Some(1_000_000_000_000_000 + i as i64 * 1_000_000 + 7_000_000),
                ClockBasis::BootRelative,
            )
        }));
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(al.basis, ClockBasis::GpsSuspect);
        assert!(al.offset.expect("o").discontinuity);
        assert!(al.offset.expect("o").bound_us >= 7_000_000);
        assert_eq!(
            order(0, 10_000_000, 5_000_000, 10_000_000),
            OrderRelation::OrderUnknown
        );
        assert_eq!(order(0, 1_000, 5_000_000, 1_000), OrderRelation::Before);
    }

    #[test]
    fn unrepresentable_alignment_abstains_without_wrapping_or_host_promotion() {
        for obs in [
            vec![ob(1, Some(i64::MIN), ClockBasis::BootRelative)],
            vec![ob(u64::MAX, Some(0), ClockBasis::BootRelative)],
            vec![
                ob(0, Some(i64::MIN), ClockBasis::BootRelative),
                ob(1, Some(i64::MAX), ClockBasis::BootRelative),
            ],
        ] {
            let device = estimate_alignment(&obs, ClockBasis::BootRelative);
            assert_eq!(device.basis, ClockBasis::GpsSuspect);
            assert!(device.offset.is_none());
            assert!(to_wall_us(&device, 1).is_none());
            let host = estimate_alignment(&obs, ClockBasis::HostReceived);
            assert_eq!(host.basis, ClockBasis::HostReceived);
            assert!(host.offset.is_none());
            assert_eq!(obs[0].clock_basis, ClockBasis::BootRelative);
        }
        // The signed endpoint itself is representable, unlike MIN minus one above.
        let al = estimate_alignment(
            &[ob(0, Some(i64::MIN), ClockBasis::BootRelative)],
            ClockBasis::BootRelative,
        );
        assert_eq!(al.offset.unwrap().offset_us, i64::MIN);
        assert_eq!(to_wall_us(&al, 0), Some((i64::MIN, 1)));
    }

    #[test]
    fn derived_wall_and_start_alignment_use_checked_wide_arithmetic() {
        let obs = vec![ob(0, None, ClockBasis::BootRelative)];
        assert!(align_by_start(&obs, i64::MAX, 1).is_none());
        assert!(align_by_start(&obs, i64::MIN, 1).is_none());
        let mut al = align_by_start(&obs, 1, 1).unwrap();
        assert_eq!(to_wall_us(&al, 42), Some((1042, 1)));
        assert!(to_wall_us(&al, i64::MAX as u64).is_none());
        al.offset.as_mut().unwrap().offset_us = i64::MIN;
        // A large unsigned boot time plus a negative offset can still be valid.
        assert_eq!(to_wall_us(&al, u64::MAX), Some((i64::MAX, 1)));
        al.offset.as_mut().unwrap().offset_us = 1;
        assert!(to_wall_us(&al, u64::MAX).is_none());
    }
}
