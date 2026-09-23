use musubi_reference_readers::{
    ClockBasis, ProfileReader, edgetx::EdgeTxCsvReader, profile::parse_profile,
};
use musubi_reference_types::FamilyProfile;

const CASE: &str = "profiles/declared/edgetx-handset-clock";

const RTC_PROFILE: &str = include_str!("fixtures/edgetx-handset-clock--rtc-date-time.profile.toml");
const COUNTER_PROFILE: &str =
    include_str!("fixtures/edgetx-handset-clock--boot-counter.profile.toml");
const ALTERNATIVES_PROFILE: &str =
    include_str!("fixtures/edgetx-handset-clock--time-alternatives.profile.toml");
const RTC_CSV: &[u8] = include_bytes!("fixtures/authored-rtc.csv");
const COUNTER_CSV: &[u8] = include_bytes!("fixtures/authored-boot-counter.csv");
const AMBIGUOUS_CSV: &[u8] = include_bytes!("fixtures/authored-ambiguous-refused.csv");
const SYNTH_PROFILE: &str = include_str!("fixtures/profile-synth-v1--candidate--profile.toml");
const SYNTH_CSV: &[u8] = include_bytes!("fixtures/sample.csv");

fn load(text: &str, name: &str) -> FamilyProfile {
    parse_profile(text, name).expect("the shipped profile must load through the existing loader")
}

#[test]
fn the_rtc_profile_yields_unknown_clock_with_no_wall_time() {
    let profile = load(RTC_PROFILE, &format!("{CASE}/rtc-date-time.profile.toml"));
    let observations = EdgeTxCsvReader.read(&profile, RTC_CSV).unwrap();
    assert_eq!(observations.len(), 4);
    for o in &observations {
        assert_eq!(o.clock_basis, ClockBasis::Unknown);
        assert!(o.wall_ms.is_none());
        assert!(o.anchor_unix_us.is_none());
        assert_eq!(o.fields.len(), 3);
    }
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
    let rtc = EdgeTxCsvReader.read(&profile, RTC_CSV).unwrap();
    assert_eq!(rtc.len(), 4);
    assert!(
        rtc.iter()
            .all(|o| o.clock_basis == ClockBasis::Unknown && o.wall_ms.is_none())
    );
    let counter = EdgeTxCsvReader.read(&profile, COUNTER_CSV).unwrap();
    assert_eq!(counter.len(), 4);
    assert!(
        counter
            .iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelative)
    );
    assert_eq!(counter[0].t_ms, 30_000);
    let failure = EdgeTxCsvReader
        .read(&profile, AMBIGUOUS_CSV)
        .expect_err("a header carrying both alternatives must be refused");
    assert!(
        format!("{failure:?}").contains("ambiguous"),
        "expected an ambiguity refusal, got {failure:?}"
    );
}

#[test]
fn a_decreasing_handset_clock_is_refused_rather_than_wrap_corrected() {
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
    let synth = load(
        SYNTH_PROFILE,
        "fixtures/profile-synth-v1--candidate--profile.toml",
    );
    assert_eq!(synth.default_clock_basis, ClockBasis::HostReceived);
    let observations = EdgeTxCsvReader.read(&synth, SYNTH_CSV).unwrap();
    assert!(!observations.is_empty());
    assert!(
        observations
            .iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelative && o.wall_ms.is_none())
    );
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
