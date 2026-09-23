#![allow(clippy::doc_markdown, clippy::expect_used, clippy::unwrap_used)]

use musubi_reference_pipeline::export::{answer_text, source_clock_line};
use musubi_reference_pipeline::{Knowledge, SourceRun, run_pipeline_with};
use musubi_reference_readers::profile::parse_profile;
use musubi_reference_scenario::{FailureKind, Family, generate, pre_demo_default};
use musubi_reference_types::ClockRateConfig;

const ROVER_BIN: &str = include_str!("../../../profiles/public/ardupilot_rover_bin.toml");
const ROVER_TLOG: &str = include_str!("../../../profiles/public/ardupilot_rover_tlog.toml");

const DECLARATION: &str = "\n[clock_rate]\nanchor_uncertainty_us = 1000\nmin_span_us = 60000000\nmax_abs_rate_ppm = 200\n";

fn refusal(source: &str, table: &str) -> String {
    parse_profile(&format!("{source}{table}"), "public")
        .expect_err("the declaration should be refused")
        .to_string()
}

#[test]
fn the_examination_is_off_until_a_profile_declares_it_and_every_declared_value_is_checked() {
    let shipped = Knowledge::repo_public();
    assert!(
        shipped
            .profiles
            .iter()
            .all(|p| p.declared_clock_rate.is_none()),
        "no shipped profile may carry a threshold of its own"
    );

    let declared = parse_profile(&format!("{ROVER_BIN}{DECLARATION}"), "public").expect("declared");
    assert_eq!(
        declared.declared_clock_rate,
        Some(ClockRateConfig {
            anchor_uncertainty_us: 1_000,
            min_span_us: 60_000_000,
            max_abs_rate_ppm: 200,
        })
    );

    for missing in [
        "\n[clock_rate]\nmin_span_us = 60000000\nmax_abs_rate_ppm = 200\n",
        "\n[clock_rate]\nanchor_uncertainty_us = 1000\nmax_abs_rate_ppm = 200\n",
        "\n[clock_rate]\nanchor_uncertainty_us = 1000\nmin_span_us = 60000000\n",
        "\n[clock_rate]\n",
    ] {
        assert!(
            parse_profile(&format!("{ROVER_BIN}{missing}"), "public").is_err(),
            "an incomplete declaration must not be completed by a default: {missing}"
        );
    }

    for (bad, why) in [
        ("anchor_uncertainty_us = 0", "a perfectly known pairing"),
        (
            "anchor_uncertainty_us = 500",
            "a half-millisecond claim from a millisecond field",
        ),
        ("anchor_uncertainty_us = -1", "a negative half-width"),
        ("anchor_uncertainty_us = 1.5", "a non-integer microsecond"),
        ("anchor_uncertainty_us = nan", "a non-finite half-width"),
        ("anchor_uncertainty_us = inf", "an infinite half-width"),
        ("min_span_us = 0", "a zero minimum span"),
        ("max_abs_rate_ppm = 0", "a zero limit"),
        ("max_abs_rate_ppm = -50", "a negative limit"),
        ("max_abs_rate_ppm = nan", "a non-finite limit"),
    ] {
        let table = DECLARATION
            .lines()
            .map(|l| {
                let key = bad.split(' ').next().expect("key");
                if l.starts_with(key) { bad } else { l }
            })
            .collect::<Vec<_>>()
            .join("\n");
        assert!(
            parse_profile(&format!("{ROVER_BIN}\n{table}\n"), "public").is_err(),
            "must refuse {why}: {bad}"
        );
    }

    let floor_case = |us: i64| {
        parse_profile(
            &format!(
                "{ROVER_BIN}\n[clock_rate]\nanchor_uncertainty_us = {us}\nmin_span_us = 60000000\nmax_abs_rate_ppm = 200\n"
            ),
            "public",
        )
    };
    assert!(
        floor_case(999).is_err(),
        "999 us is finer than GPS.GMS resolves"
    );
    assert_eq!(
        floor_case(1_000)
            .expect("exactly the quantization floor is accepted")
            .declared_clock_rate
            .expect("declared")
            .anchor_uncertainty_us,
        1_000
    );
    assert_eq!(
        floor_case(250_000)
            .expect("a larger declared error is the operator's to make")
            .declared_clock_rate
            .expect("declared")
            .anchor_uncertainty_us,
        250_000
    );
    let too_fine = refusal(
        ROVER_BIN,
        "\n[clock_rate]\nanchor_uncertainty_us = 1\nmin_span_us = 60000000\nmax_abs_rate_ppm = 200\n",
    );
    assert!(too_fine.contains("at least 1000 us"), "{too_fine}");
    assert!(
        too_fine.contains("not evidence that the pairing latency is bounded"),
        "the refusal must not imply 1 ms bounds the real pairing error: {too_fine}"
    );

    assert!(
        parse_profile(
            &format!("{ROVER_BIN}{DECLARATION}max_abs_rate_ppb = 5\n"),
            "public"
        )
        .is_err()
    );

    let no_path = refusal(ROVER_TLOG, DECLARATION);
    assert!(no_path.contains("mavlink_tlog"), "{no_path}");
    assert!(no_path.contains("no paired in-log anchors"), "{no_path}");
}

#[test]
fn a_declared_examination_reaches_the_clock_report_with_its_basis_and_its_limits() {
    let mut s = pre_demo_default(11);
    s.injections.clear();
    s.inject(FailureKind::GnssDegradation, 120_000, 40_000, Family::Ugv);
    let tl = generate(&s);

    let mut knowledge = Knowledge::repo_public();
    let declared =
        parse_profile(&format!("{ROVER_BIN}{DECLARATION}"), "public").expect("declared profile");
    for p in &mut knowledge.profiles {
        if p.profile_id == declared.profile_id {
            *p = declared.clone();
        }
    }
    let out = run_pipeline_with(&tl, &knowledge);

    let bin: &SourceRun = out
        .assets
        .iter()
        .flat_map(|a| &a.sources)
        .find(|s| s.profile.profile_id == "ardupilot_rover_bin")
        .expect("the DataFlash source");
    let report = bin.clock_rate.expect("the declared examination ran");
    assert_eq!(report.config.max_abs_rate_ppm, 200);

    let line = source_clock_line(bin);
    let text = answer_text(&out, &knowledge, None);
    assert!(text.contains(&line), "the clock line must reach the report");
    assert!(line.contains("relative clock rate"), "{line}");

    let drift_ppm = i64::from(
        s.assets
            .iter()
            .find(|a| a.family == Family::Ugv)
            .expect("the ugv asset")
            .drift_ppm,
    );
    assert_eq!(drift_ppm, 20, "the fixture's injected drift");
    let musubi_reference_types::ClockRateOutcome::Observed {
        low_ppm,
        high_ppm,
        class,
    } = report.outcome
    else {
        panic!("paired GPS anchors are present, so an interval is expected: {report:?}");
    };
    assert!(low_ppm < high_ppm, "{low_ppm}..{high_ppm}");
    assert!(
        low_ppm <= -drift_ppm && -drift_ppm <= high_ppm,
        "the interval {low_ppm}..{high_ppm} ppm must contain the injected {}",
        -drift_ppm
    );
    let half_width = 2 * 1_000 * 1_000_000 / (report.span_us / 1_000);
    assert!(
        high_ppm - low_ppm <= 2 * half_width + 2,
        "{low_ppm}..{high_ppm} is wider than the declared pairing justifies"
    );
    assert_eq!(
        class,
        musubi_reference_types::ClockRateClass::WithinDeclaredLimit,
        "20 ppm sits well inside the declared ±200 ppm"
    );

    assert!(
        line.contains(&format!("{low_ppm}..{high_ppm} ppm")),
        "{line}"
    );
    assert!(line.contains("ppm of recorded device time"), "{line}");
    assert!(
        line.contains("positive = declared reference advanced more"),
        "{line}"
    );
    assert!(line.contains("not attributed to either clock"), "{line}");
    assert!(line.contains("within-declared-limit"), "{line}");
    assert!(line.contains("vs declared ±200 ppm"), "{line}");
    assert!(line.contains("declared pairing ±1000 us"), "{line}");
    assert!(
        line.contains("averaged first anchor to last (not per-interval stability)"),
        "{line}"
    );
    assert!(
        line.contains("floor of what the field can resolve, not a bound on pairing latency"),
        "{line}"
    );

    assert!(
        line.contains("boot 0 ≈") || line.contains("boot-relative"),
        "{line}"
    );
    for s in out.assets.iter().flat_map(|a| &a.sources) {
        if s.profile.profile_id != "ardupilot_rover_bin" {
            assert!(s.clock_rate.is_none(), "{}", s.profile.profile_id);
            assert!(!source_clock_line(s).contains("relative clock rate"));
        }
    }
}
