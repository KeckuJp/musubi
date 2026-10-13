use musubi_reference_readers::{
    FieldValue, ProfileReader, field, profile::parse_profile, tlog::TlogReader, tlog_sender_batch,
};

fn profile(sys: u8, comp: u8) -> musubi_reference_types::FamilyProfile {
    let source = include_str!("fixtures/unknown-adapter--tlog-state--selected-sender-profile.toml")
        .replace("system_id = 42", &format!("system_id = {sys}"))
        .replace("component_id = 3", &format!("component_id = {comp}"));
    parse_profile(&source, "authored").unwrap()
}

fn record(sys: u8, comp: u8, state: u8) -> Vec<u8> {
    let mut frame = vec![0xfe, 9, 7, sys, comp, 0, 0, 0, 0, 0, 2, 3, 0, state, 3];
    let mut crc = 0xffff_u16;
    for b in frame[1..].iter().copied().chain([50]) {
        let mut tmp = b ^ (crc & 0xff) as u8;
        tmp ^= tmp << 4;
        crc = (crc >> 8) ^ ((tmp as u16) << 8) ^ ((tmp as u16) << 3) ^ ((tmp as u16) >> 4);
    }
    frame.extend_from_slice(&crc.to_le_bytes());
    [1_700_000_000_000_000_u64.to_be_bytes().as_slice(), &frame].concat()
}

#[test]
fn distinct_reported_senders_reuse_common_meaning_without_merging() {
    for records in [
        vec![
            record(42, 3, 3),
            record(42, 4, 4),
            record(7, 1, 5),
            record(42, 3, 6),
        ],
        vec![record(42, 4, 2), record(42, 3, 7)],
    ] {
        let bytes = records.concat();
        let profiles = [profile(42, 3), profile(42, 4)];
        let result = tlog_sender_batch::read(&profiles, &bytes).unwrap();
        assert_eq!(result.total_records, records.len());
        assert_eq!(
            result.selected_records + result.unselected_records,
            records.len()
        );
        assert_eq!(result.unselected_records, usize::from(records.len() == 4));
        assert_eq!(result.input_sha256.len(), 64);
        for (selected, profile) in result.senders.iter().zip(&profiles) {
            assert_eq!(&selected.profile, profile);
            let old = TlogReader.read(profile, &bytes).unwrap();
            assert_eq!(selected.observations, old);
            assert!(
                selected
                    .observations
                    .iter()
                    .all(|o| field(o, "MAVLINK.component_id")
                        == Some(&FieldValue::I64(
                            profile.declared_sender.unwrap().component_id.into()
                        )))
            );
        }
    }
}

#[test]
fn explicit_budget_override_preserves_all_two_thousand_records() {
    let bytes = [record(42, 3, 3), record(42, 4, 4)].concat().repeat(1000);
    let profiles = [profile(42, 3), profile(42, 4)];
    let budget = tlog_sender_batch::Limits {
        input_bytes: bytes.len(),
        aggregate_read_bytes: bytes.len() * 2,
        observations: 1999,
    };
    assert!(tlog_sender_batch::read_with_limits(&profiles, &bytes, budget).is_err());
    let result = tlog_sender_batch::read_with_limits(
        &profiles,
        &bytes,
        tlog_sender_batch::Limits {
            observations: 2000,
            ..budget
        },
    )
    .unwrap();
    assert_eq!(result.total_records, 2000);
    assert_eq!(result.selected_records, 2000);
    assert_eq!(result.unselected_records, 0);
    assert!(result.senders.iter().all(|s| s.observations.len() == 1000));
}

#[test]
fn ambiguous_absent_invalid_and_excessive_batches_are_refused() {
    let bytes = record(42, 3, 3);
    assert!(tlog_sender_batch::read(&[], &bytes).is_err());
    assert!(tlog_sender_batch::read(&[profile(42, 3), profile(42, 3)], &bytes).is_err());
    assert!(tlog_sender_batch::read(&[profile(42, 4)], &bytes).is_err());
    let mut missing = profile(42, 3);
    missing.declared_sender = None;
    assert!(tlog_sender_batch::read(&[missing], &bytes).is_err());
    assert!(tlog_sender_batch::read(&vec![profile(42, 3); 9], &bytes).is_err());
    assert!(tlog_sender_batch::read(&[profile(42, 3)], &vec![0; 16 * 1024 * 1024 + 1]).is_err());
    let many: Vec<_> = (1..=8).map(|c| profile(42, c)).collect();
    assert!(tlog_sender_batch::read(&many, &vec![0; 8 * 1024 * 1024 + 1]).is_err());
    let mut corrupt = bytes;
    *corrupt.last_mut().unwrap() ^= 1;
    assert!(tlog_sender_batch::read(&[profile(42, 3)], &corrupt).is_err());
}
