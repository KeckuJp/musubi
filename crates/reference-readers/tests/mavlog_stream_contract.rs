//! Independently authored framing/meaning contract; synthetic input, no live device.
use musubi_reference_readers::{
    ClockBasis, FamilyProfile,
    mavlog_json::MavlogJsonReader,
    mavlog_stream::{FrameResult, GpsJsonlStream, MAX_FRAME_BYTES},
    profile::parse_profile,
};
use musubi_types::PlatformDomain;

fn profile() -> FamilyProfile {
    parse_profile(
        include_str!("../../../profiles/declared/arduplane-jsonl/profile.toml"),
        "contract",
    )
    .unwrap()
}

fn gps(time: u64) -> Vec<u8> {
    format!(r#"{{"meta":{{"type":"GPS","timestamp":1700000000.0}},"data":{{"TimeUS":{time},"Lat":35.25,"Lng":139.75,"Alt":123.5,"future":"観測"}}}}"#).into_bytes()
}

#[test]
fn a_complete_frame_emits_before_finish_and_matches_existing_meaning() {
    let raw = gps(2001);
    let expected = MavlogJsonReader.read_report(&profile(), &raw).unwrap();
    let mut stream = GpsJsonlStream::new(profile(), "source-A".into()).unwrap();
    let mut events = Vec::new();
    for byte in &raw {
        stream
            .push(&[*byte], Some(1700000000123), |e| events.push(e))
            .unwrap();
        assert!(
            events.is_empty(),
            "partial UTF-8/JSON is not a complete frame"
        );
    }
    stream
        .push(b"\r", Some(1700000000123), |e| events.push(e))
        .unwrap();
    assert!(events.is_empty());
    stream
        .push(b"\n", Some(1700000000123), |e| events.push(e))
        .unwrap();
    assert_eq!(events.len(), 1, "must not wait for EOF");
    let event = &events[0];
    assert_eq!(event.source_id, "source-A");
    assert_eq!(event.frame_index, 1);
    assert_eq!(event.arrival_unix_ms, Some(1700000000123));
    assert_eq!(event.platform_domain, PlatformDomain::Unknown);
    assert_eq!(event.source_bytes, [raw.as_slice(), b"\r"].concat());
    let FrameResult::Observation(actual) = &event.result else {
        panic!("valid GPS was not observed")
    };
    assert_eq!(actual.fields, expected.observations[0].fields);
    assert_eq!(actual.t_boot_us, Some(2001));
    assert_eq!(actual.clock_basis, ClockBasis::BootRelative);
    assert!(
        actual.wall_ms.is_none(),
        "host arrival is not a trusted device clock"
    );
    stream.finish(|e| events.push(e)).unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(stream.counts().frames, stream.counts().observations);
}

#[test]
fn equal_and_reset_clock_records_are_not_lost_or_reordered() {
    let mut stream = GpsJsonlStream::new(profile(), "source-A".into()).unwrap();
    let mut times = Vec::new();
    for time in [2001, 2001, 1] {
        let line = [gps(time), b"\n".to_vec()].concat();
        stream
            .push(&line, None, |e| {
                let FrameResult::Observation(o) = e.result else {
                    panic!("valid repeated/reset clock rejected")
                };
                times.push(o.t_boot_us);
            })
            .unwrap();
    }
    assert_eq!(times, vec![Some(2001), Some(2001), Some(1)]);
    assert_eq!(stream.counts().observations, 3);
}

#[test]
fn oversized_input_is_bounded_accounted_and_does_not_poison_the_next_frame() {
    let mut stream = GpsJsonlStream::new(profile(), "source-A".into()).unwrap();
    let chunk = vec![b'x'; MAX_FRAME_BYTES];
    let mut events = Vec::new();
    // More than 16 MiB over a finite test session: no arbitrary whole-stream capacity cap.
    for _ in 0..257 {
        stream.push(&chunk, None, |e| events.push(e)).unwrap();
        assert!(stream.buffered_bytes() <= MAX_FRAME_BYTES);
    }
    stream.push(b"\n", None, |e| events.push(e)).unwrap();
    stream
        .push(&[gps(10), b"\n".to_vec()].concat(), None, |e| {
            events.push(e)
        })
        .unwrap();
    assert_eq!(events.len(), 2);
    assert!(matches!(events[0].result, FrameResult::Oversized));
    assert_eq!(events[0].frame_bytes, (257 * MAX_FRAME_BYTES) as u64);
    assert!(events[0].source_bytes.len() <= MAX_FRAME_BYTES);
    assert!(matches!(events[1].result, FrameResult::Observation(_)));
    assert_eq!(stream.counts().oversized_frames, 1);
    assert_eq!(stream.counts().observations, 1);
    assert_eq!(stream.counts().frames, 2);
}

#[test]
fn unsupported_invalid_and_incomplete_are_visible_without_fake_observations() {
    let mut stream = GpsJsonlStream::new(profile(), "source-A".into()).unwrap();
    let mut events = Vec::new();
    stream.push(b"{\"meta\":{\"type\":\"MSG\"},\"data\":{\"Message\":\"authored\"}}\nnot-json\n{\"meta\":", None, |e| events.push(e)).unwrap();
    stream.finish(|e| events.push(e)).unwrap();
    assert_eq!(events.len(), 3);
    assert!(matches!(events[0].result, FrameResult::Unsupported(_)));
    assert!(matches!(events[1].result, FrameResult::Invalid(_)));
    assert!(matches!(events[2].result, FrameResult::IncompleteEof));
    let c = stream.counts();
    assert_eq!(c.observations, 0);
    assert_eq!(
        c.frames,
        c.unsupported_frames + c.invalid_frames + c.incomplete_frames
    );
}
