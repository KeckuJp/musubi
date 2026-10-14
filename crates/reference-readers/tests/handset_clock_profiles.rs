use musubi_reference_readers::{
    ClockBasis, ProfileReader, edgetx::EdgeTxCsvReader, profile::parse_profile,
};
use musubi_reference_types::FamilyProfile;

const CASE: &str = "profiles/declared/edgetx-handset-clock";

// The exact shipped files, embedded at compile time so the test cannot drift from the case.
const RTC_PROFILE: &str =
    include_str!("../../../profiles/declared/edgetx-handset-clock/rtc-date-time.profile.toml");
const COUNTER_PROFILE: &str =
    include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml");
const ALTERNATIVES_PROFILE: &str =
    include_str!("../../../profiles/declared/edgetx-handset-clock/time-alternatives.profile.toml");
const RTC_CSV: &[u8] =
    include_bytes!("../../../profiles/declared/edgetx-handset-clock/authored-rtc.csv");
const COUNTER_CSV: &[u8] =
    include_bytes!("../../../profiles/declared/edgetx-handset-clock/authored-boot-counter.csv");
const AMBIGUOUS_CSV: &[u8] = include_bytes!(
    "../../../profiles/declared/edgetx-handset-clock/authored-ambiguous-refused.csv"
);
// The shipped synthetic candidate, used only to show at runtime what its declaration actually does.
const SYNTH_PROFILE: &str =
    include_str!("../../../examples/saved-inputs/authored-handset-profile.toml");
const SYNTH_CSV: &[u8] = include_bytes!("../../../examples/saved-inputs/authored-handset.csv");

fn load(text: &str, name: &str) -> FamilyProfile {
    parse_profile(text, name).expect("the shipped profile must load through the existing loader")
}

#[test]
fn the_rtc_profile_yields_unknown_clock_with_no_wall_time() {
    let profile = load(RTC_PROFILE, &format!("{CASE}/rtc-date-time.profile.toml"));
    let observations = EdgeTxCsvReader.read(&profile, RTC_CSV).unwrap();
    assert_eq!(observations.len(), 4);
    for o in &observations {
        // The RTC pair carries no zone, so order only: never a wall-clock anchor.
        assert_eq!(o.clock_basis, ClockBasis::Unknown);
        assert!(o.wall_ms.is_none());
        assert!(o.anchor_unix_us.is_none());
        // Every source column is kept; the header is Date,Time,RQly(%).
        assert_eq!(o.fields.len(), 3);
    }
    // Two rows share one RTC instant in the fixture and both survive, in file order.
    assert_eq!(observations[1].t_ms, observations[2].t_ms);
    assert!(observations[0].t_ms < observations[1].t_ms);
    assert!(observations[2].t_ms < observations[3].t_ms);
}

#[test]
fn the_counter_profile_scales_the_tick_once_and_reports_boot_relative() {
    let profile = load(
        COUNTER_PROFILE,
        &format!("{CASE}/boot-counter.profile.toml"),
    );
    let observations = EdgeTxCsvReader.read(&profile, COUNTER_CSV).unwrap();
    assert_eq!(observations.len(), 4);
    // authored ticks: 3000, 3010, 3010, 3020. t_ms = tick * 10, applied exactly once - a second
    // application anywhere would give 300000, and half of one would give 3000.
    assert_eq!(
        observations.iter().map(|o| o.t_ms).collect::<Vec<_>>(),
        vec![30_000, 30_100, 30_100, 30_200]
    );
    for o in &observations {
        assert_eq!(o.clock_basis, ClockBasis::BootRelative);
        assert!(o.wall_ms.is_none());
        assert_eq!(o.fields.len(), 2);
    }
}

#[test]
fn the_alternatives_profile_reads_either_header_and_refuses_both_at_once() {
    let profile = load(
        ALTERNATIVES_PROFILE,
        &format!("{CASE}/time-alternatives.profile.toml"),
    );
    // Alternative one: the RTC header alone.
    let rtc = EdgeTxCsvReader.read(&profile, RTC_CSV).unwrap();
    assert_eq!(rtc.len(), 4);
    assert!(
        rtc.iter()
            .all(|o| o.clock_basis == ClockBasis::Unknown && o.wall_ms.is_none())
    );
    // Alternative two: the counter header alone, with the same single scaling.
    let counter = EdgeTxCsvReader.read(&profile, COUNTER_CSV).unwrap();
    assert_eq!(counter.len(), 4);
    assert!(
        counter
            .iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelative)
    );
    assert_eq!(counter[0].t_ms, 30_000);
    // Both headers present is ambiguous and refused, not resolved by preferring one.
    let failure = EdgeTxCsvReader
        .read(&profile, AMBIGUOUS_CSV)
        .expect_err("a header carrying both alternatives must be refused");
    assert!(
        format!("{failure:?}").contains("ambiguous"),
        "expected an ambiguity refusal, got {failure:?}"
    );
}

/// The pinned EdgeTX 65bb97db / OpenTX fb8622dd logs.cpp write `Time,` (then `%d` tmr10ms) for an
/// RTC-less build. The shipped alternatives profile must read that header, not only `tmr10ms`.
#[test]
fn the_alternatives_profile_reads_the_pinned_writer_rtc_less_time_header() {
    let profile = load(
        ALTERNATIVES_PROFILE,
        &format!("{CASE}/time-alternatives.profile.toml"),
    );
    let input = b"Time,RQly(%),RxBt(V),TxBat(V)\n3000,98,12.3,7.9\n3000,97,,7.9\n3010,0,0,7.8\n";
    let rows = EdgeTxCsvReader.read(&profile, input.as_slice()).unwrap();
    assert_eq!(
        rows.iter().map(|o| o.t_ms).collect::<Vec<_>>(),
        vec![30_000, 30_000, 30_100]
    );
    assert!(
        rows.iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelative
                && o.wall_ms.is_none()
                && o.fields.len() == 4)
    );
    for bad in [
        b"Time,RQly(%)\n3000,98\n10,97\n".as_slice(),
        b"Date,Time,tmr10ms,RQly(%)\n2026-09-01,10:11:12.340,1,98\n",
        b"Time,tmr10ms,RQly(%)\n1,1,98\n",
    ] {
        assert!(EdgeTxCsvReader.read(&profile, bad).is_err());
    }
}

#[test]
fn a_decreasing_handset_clock_is_refused_rather_than_wrap_corrected() {
    // A second power cycle inside one file: the counter restarts. The reader refuses; it never
    // stitches two sessions or guesses a wrap.
    let profile = load(
        COUNTER_PROFILE,
        &format!("{CASE}/boot-counter.profile.toml"),
    );
    let two_sessions = b"tmr10ms,RQly(%)\n3000,98\n10,97\n";
    let failure = EdgeTxCsvReader
        .read(&profile, two_sessions.as_slice())
        .expect_err("a decreasing handset clock must be refused");
    assert!(
        format!("{failure:?}").contains("decreasing handset clock"),
        "expected the session-split refusal, got {failure:?}"
    );
}

#[test]
fn the_reader_assigns_the_basis_even_when_a_profile_declares_another() {
    // The shipped synthetic candidate declares host_received and selects tmr10ms. That declaration
    // is not a wrong clock on the output: the reader assigns the basis itself, so what reaches an
    // Observation is boot_relative either way. This is what makes the new profiles a packaging and
    // usability improvement rather than a defect fix.
    let synth = load(
        SYNTH_PROFILE,
        "examples/saved-inputs/authored-handset-profile.toml",
    );
    assert_eq!(synth.default_clock_basis, ClockBasis::HostReceived);
    let observations = EdgeTxCsvReader.read(&synth, SYNTH_CSV).unwrap();
    assert!(!observations.is_empty());
    assert!(
        observations
            .iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelative && o.wall_ms.is_none())
    );
    // The case profile declares what the reader will assign, so declaration and behaviour agree.
    let declared = load(
        COUNTER_PROFILE,
        &format!("{CASE}/boot-counter.profile.toml"),
    );
    assert_eq!(declared.default_clock_basis, ClockBasis::BootRelative);
    let same = EdgeTxCsvReader.read(&declared, SYNTH_CSV).unwrap();
    assert_eq!(
        same.iter().map(|o| o.clock_basis).collect::<Vec<_>>(),
        observations
            .iter()
            .map(|o| o.clock_basis)
            .collect::<Vec<_>>()
    );
}
