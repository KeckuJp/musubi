use musubi_decoded_csv::ParseOptions;
use musubi_reference_readers::{
    ChannelId, ClockBasis, Observation,
    catalog::parse_catalog,
    cause::{SourceInput, Window, extract_cues, match_signatures},
    profile::parse_profile,
    telemetry_csv::TelemetryCsvReader,
    time_align,
};
use musubi_reference_types::{
    CauseOutcome, Family, OffsetEstimate, OffsetSource, TimeAlignment, UnknownReason,
};

/// The caller's own declared anchoring of a boot-relative export to wall time. It is an assumption
/// they state, never something this file measures, and without it no cue window reads anything.
fn declared_alignment() -> TimeAlignment {
    TimeAlignment {
        basis: ClockBasis::BootRelativeOffsetEstimated,
        offset: Some(OffsetEstimate {
            offset_us: 0,
            bound_us: 1_000_000,
            source: OffsetSource::InLogAnchor,
            anchors: 1,
            discontinuity: false,
        }),
    }
}

/// The shipped boot-relative case profile, not a copy shaped for this test. This format accepts
/// only `unknown` or `boot_relative`, and a `wall_ms` -- which every cue window reads -- appears
/// only when the caller ALSO supplies the offset estimate built below.
fn profile() -> musubi_reference_readers::FamilyProfile {
    parse_profile(
        include_str!("../../../profiles/declared/link-cue-fields/profile-boot-relative.toml"),
        "private",
    )
    .expect("the shipped boot-relative link-cue-fields profile parses")
}

/// The shipped sibling for a caller who declares an unknown clock.
fn unknown_clock_profile() -> musubi_reference_readers::FamilyProfile {
    parse_profile(
        include_str!("../../../profiles/declared/link-cue-fields/profile.toml"),
        "private",
    )
    .expect("the shipped link-cue-fields profile parses")
}

/// The shipped signature catalog, not a fixture.
fn catalog() -> musubi_reference_types::SignatureCatalog {
    parse_catalog(include_str!(
        "../../../profiles/public/catalog/failure_signatures.toml"
    ))
    .expect("the shipped failure signature catalog parses")
}

/// Exactly the header the bridge emits, so a drift in either would fail here rather than pass.
const HEADER: &str = "record_time_us,RQly(%),1RSS(dB),RADIO_STATUS.rssi,RADIO_STATUS.noise,\
mapped_cue_inputs,cue_inputs_carried,cue_inputs_absent,undetermined_cue_tests,\
declared_clock_basis,windowed_evaluation_input,source_mapping_hex,measurement_basis,source_sha256";

fn bridged(rows: &str) -> Vec<Observation> {
    let input = format!("{HEADER}\n{rows}");
    let read = TelemetryCsvReader
        .read_with_options(
            &profile(),
            input.as_bytes(),
            ParseOptions {
                allow_equal_time: true,
                preserve_nonfinite_as_text: false,
                ..Default::default()
            },
        )
        .expect("the bridge output is readable by the common reader");
    time_align::promote(&read, &declared_alignment())
}

/// One row of the bridge's own output shape. Only the carried names vary between cases.
fn row(time_us: u64, quality: &str, dbm: &str) -> String {
    format!(
        "{time_us},{quality},{dbm},,,RQly(%)+1RSS(dB),RQly(%)+1RSS(dB),\
RADIO_STATUS.rssi+RADIO_STATUS.noise,REPORTED_RSSI_RATIO_TEST+REPORTED_NOISE_TEST,boot_relative,\
BOOT_RELATIVE_WITH_A_CALLER_SUPPLIED_OFFSET,hex:7b7d,BASIS,sha256:0\n"
    )
}

/// The analysis window; every authored row below sits in its prelude look-back.
const WINDOW: Window = Window {
    start_ms: 4_000,
    end_ms: 9_000,
};

fn cues_for(rows: &str) -> musubi_reference_readers::cause::CueSet {
    let obs = bridged(rows);
    let profile = profile();
    extract_cues(
        &[SourceInput {
            profile: &profile,
            obs: &obs,
            negatives: &[],
            alignment: declared_alignment(),
        }],
        WINDOW,
    )
}

fn has(cues: &musubi_reference_readers::cause::CueSet, id: &str) -> bool {
    cues.present
        .iter()
        .any(|(ch, name)| *ch == ChannelId::LinkStats && name == id)
}

#[test]
fn a_carried_percent_is_read_by_the_existing_link_quality_test_in_its_own_unit() {
    // 0.98 / 0.96 / 0.97 as the adopted link converter reports them are fractions; the bridge
    // carried them under `RQly(%)` as 98 / 96 / 97, which is the unit this existing test reads.
    let cues = cues_for(&format!(
        "{}{}{}",
        row(1_000_000, "98", "-70"),
        row(2_000_000, "96", "-72"),
        row(3_000_000, "97", "-71")
    ));
    assert!(
        cues.observed_channels.contains(&ChannelId::LinkStats),
        "the case profile declares the link_stats channel, so it is observable"
    );
    // The existing threshold is "< 90". None of these is below it, so the existing rule reports the
    // absence of a prelude -- it does not report a degradation.
    assert!(has(&cues, "no_prelude"), "cues were {:?}", cues.present);
    assert!(!has(&cues, "prelude_link_stats"));
}

#[test]
fn the_existing_rule_needs_more_samples_to_say_nothing_is_wrong_than_to_say_something_is() {
    // Two healthy samples produce NO link cue at all: the existing rule requires at least three
    // before it will state `no_prelude`, while a single degraded sample is enough for
    // `prelude_link_stats`. That asymmetry is the adopted module's, not this bridge's, and it is
    // asserted here so a caller does not read two quiet samples as a quiet link.
    let quiet = cues_for(&format!(
        "{}{}",
        row(1_000_000, "98", "-70"),
        row(2_000_000, "96", "-72")
    ));
    assert!(!has(&quiet, "no_prelude") && !has(&quiet, "prelude_link_stats"));

    let one_bad = cues_for(&row(1_000_000, "40", "-70"));
    assert!(has(&one_bad, "prelude_link_stats"), "{:?}", one_bad.present);
}

#[test]
fn the_same_reading_left_as_a_fraction_fires_that_test_falsely() {
    // The counterexample, and the whole reason the bridge checks units. If the very same reported
    // reading reaches this field name unscaled -- 0.98 instead of 98 -- the existing "< 90" test
    // matches every ordinary row and manufactures a degradation cue out of a healthy link.
    let cues = cues_for(&format!(
        "{}{}",
        row(1_000_000, "0.98", "-70"),
        row(2_000_000, "0.96", "-72")
    ));
    assert!(
        has(&cues, "prelude_link_stats"),
        "a raw fraction under a percent field name fires the existing test: {:?}",
        cues.present
    );
    assert!(!has(&cues, "no_prelude"));
    // The bridge refuses to produce this file: `RQly(%)` is read in percent, and a source the
    // caller declares as a fraction is carried only by the one declared scale. This case is built
    // by hand here precisely to show what that refusal is protecting.
}

#[test]
fn a_link_only_capture_reaches_no_candidate_and_names_the_channel_it_never_observed() {
    // A genuinely degraded reported link: below the existing threshold, so the prelude cue is
    // present and the strongest link-side evidence this file can supply is supplied.
    let cues = cues_for(&format!(
        "{}{}",
        row(1_000_000, "40", "-104"),
        row(2_000_000, "35", "-106")
    ));
    assert!(has(&cues, "prelude_link_stats"), "{:?}", cues.present);

    // The family is the caller's declaration, not something this file establishes.
    let outcome = match_signatures(&catalog(), Family::Fpv, &cues);

    // `rf_link_loss` requires `link_stats:prelude_link_stats` AND `rc:rc_stop`. The RC channel is
    // not observed at all here, so the existing rule leaves that signature's match UNDEFINED rather
    // than treating the absence as a refutation -- and the outcome names why.
    match outcome {
        CauseOutcome::Unknown { reason } => assert_eq!(
            reason,
            UnknownReason::ChannelNotObserved,
            "a link-only capture is unknown because a required channel was never observed"
        ),
        other => panic!("a link measurement alone must not reach a candidate; got {other:?}"),
    }

    // Nothing here distinguishes interference, obstruction, range or an antenna fault, and nothing
    // in the shipped catalog claims to: no signature names any of the four.
    let catalog = catalog();
    for wanted in ["interference", "obstruction", "range", "antenna"] {
        assert!(
            !catalog
                .signatures
                .iter()
                .any(|s| s.signature_id.contains(wanted) || s.kind.as_str().contains(wanted)),
            "the shipped catalog must not claim to separate {wanted}"
        );
    }
}

#[test]
fn an_unknown_clock_supplies_no_windowed_input_at_all_and_that_is_not_a_healthy_link() {
    // The same degraded readings as above, read with the sibling profile a caller uses when they
    // declare an unknown clock. Every cue test in the adopted module is decided inside a wall-clock
    // window, and the common reader populates that time only on a host axis -- so this file reaches
    // the evaluation with nothing at all.
    let rows = format!(
        "{}{}",
        row(1_000_000, "40", "-104"),
        row(2_000_000, "35", "-106")
    );
    let input = format!("{HEADER}\n{rows}");
    let profile = unknown_clock_profile();
    let read = TelemetryCsvReader
        .read_with_options(
            &profile,
            input.as_bytes(),
            ParseOptions {
                allow_equal_time: true,
                preserve_nonfinite_as_text: false,
                ..Default::default()
            },
        )
        .expect("the same bytes are still readable");
    assert!(
        read.iter().all(|o| o.wall_ms.is_none()),
        "an unknown basis carries no wall-clock axis"
    );
    let cues = extract_cues(
        &[SourceInput {
            profile: &profile,
            obs: &read,
            negatives: &[],
            alignment: TimeAlignment {
                basis: ClockBasis::Unknown,
                offset: None,
            },
        }],
        WINDOW,
    );
    // Not one link cue -- not even `no_prelude`, which would have been a statement about the link.
    assert!(
        !has(&cues, "prelude_link_stats") && !has(&cues, "no_prelude"),
        "an unqualified clock yields no link cue whatsoever: {:?}",
        cues.present
    );
    // The channel is still declared observable, so the emptiness is an absent input and not a
    // missing channel. This is exactly why the bridge states `windowed_evaluation_input` on every
    // row: without it, "no cues" here is indistinguishable from a healthy link.
    assert!(cues.observed_channels.contains(&ChannelId::LinkStats));
}

#[test]
fn an_out_of_range_percent_would_have_injected_a_false_degradation_cue() {
    // The numeric defect root found, shown end to end in the real module. A percent of -1 is below
    // the existing "< 90" test, so if it reached the cue field it would manufacture a degradation
    // out of a reading that is not a percentage at all.
    let injected = cues_for(&format!(
        "{}{}{}",
        row(1_000_000, "-1", "-70"),
        row(2_000_000, "98", "-70"),
        row(3_000_000, "97", "-70")
    ));
    assert!(
        has(&injected, "prelude_link_stats"),
        "an out-of-range percent fires the existing test: {:?}",
        injected.present
    );

    // The bridge never emits that: a value outside its own unit's range is carried as unavailable,
    // so the cell is blank and the existing test sees only the readings that are percentages.
    let bridged = cues_for(&format!(
        "{}{}{}",
        row(1_000_000, "", "-70"),
        row(2_000_000, "98", "-70"),
        row(3_000_000, "97", "-70")
    ));
    assert!(
        !has(&bridged, "prelude_link_stats"),
        "{:?}",
        bridged.present
    );
}
