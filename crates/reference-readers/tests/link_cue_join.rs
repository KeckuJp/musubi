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

fn profile() -> musubi_reference_readers::FamilyProfile {
    parse_profile(
        include_str!("fixtures/unknown-adapter--link-cue-fields--profile-boot-relative.toml"),
        "private",
    )
    .expect("the shipped boot-relative link-cue-fields profile parses")
}

fn unknown_clock_profile() -> musubi_reference_readers::FamilyProfile {
    parse_profile(
        include_str!("fixtures/unknown-adapter--link-cue-fields--profile.toml"),
        "private",
    )
    .expect("the shipped link-cue-fields profile parses")
}

fn catalog() -> musubi_reference_types::SignatureCatalog {
    parse_catalog(include_str!(
        "../../../profiles/public/catalog/failure_signatures.toml"
    ))
    .expect("the shipped failure signature catalog parses")
}

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
            },
        )
        .expect("the bridge output is readable by the common reader");
    time_align::promote(&read, &declared_alignment())
}

fn row(time_us: u64, quality: &str, dbm: &str) -> String {
    format!(
        "{time_us},{quality},{dbm},,,RQly(%)+1RSS(dB),RQly(%)+1RSS(dB),\
RADIO_STATUS.rssi+RADIO_STATUS.noise,REPORTED_RSSI_RATIO_TEST+REPORTED_NOISE_TEST,boot_relative,\
BOOT_RELATIVE_WITH_A_CALLER_SUPPLIED_OFFSET,hex:7b7d,BASIS,sha256:0\n"
    )
}

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
    assert!(has(&cues, "no_prelude"), "cues were {:?}", cues.present);
    assert!(!has(&cues, "prelude_link_stats"));
}

#[test]
fn the_existing_rule_needs_more_samples_to_say_nothing_is_wrong_than_to_say_something_is() {
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
}

#[test]
fn a_link_only_capture_reaches_no_candidate_and_names_the_channel_it_never_observed() {
    let cues = cues_for(&format!(
        "{}{}",
        row(1_000_000, "40", "-104"),
        row(2_000_000, "35", "-106")
    ));
    assert!(has(&cues, "prelude_link_stats"), "{:?}", cues.present);

    let outcome = match_signatures(&catalog(), Family::Fpv, &cues);

    match outcome {
        CauseOutcome::Unknown { reason } => assert_eq!(
            reason,
            UnknownReason::ChannelNotObserved,
            "a link-only capture is unknown because a required channel was never observed"
        ),
        other => panic!("a link measurement alone must not reach a candidate; got {other:?}"),
    }

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
    assert!(
        !has(&cues, "prelude_link_stats") && !has(&cues, "no_prelude"),
        "an unqualified clock yields no link cue whatsoever: {:?}",
        cues.present
    );
    assert!(cues.observed_channels.contains(&ChannelId::LinkStats));
}

#[test]
fn an_out_of_range_percent_would_have_injected_a_false_degradation_cue() {
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
