use musubi_miniseed2::{ParseError, parse};
mod support;
use support::{packed_record, put16, put32, record};

#[test]
fn all_signed_packing_widths_and_cross_record_difference() {
    for enc in [10, 11] {
        assert_eq!(parse(&record(enc)).unwrap()[0].samples, [100, 102, 99, 103]);
    }
    for (enc, width, count, code, dnib) in [
        (10, 8, 4, 1, None),
        (10, 16, 2, 2, None),
        (10, 32, 1, 3, None),
        (11, 8, 4, 1, None),
        (11, 30, 1, 2, Some(1)),
        (11, 15, 2, 2, Some(2)),
        (11, 10, 3, 2, Some(3)),
        (11, 6, 5, 3, Some(0)),
        (11, 5, 6, 3, Some(1)),
        (11, 4, 7, 3, Some(2)),
    ] {
        let (bytes, expected) = packed_record(enc, width, count, code, dnib);
        let records = parse(&bytes).unwrap();
        assert_eq!(
            records[0].samples, expected,
            "encoding {enc}, width {width}"
        );
        assert_eq!(records[0].sample_count_decoded, expected.len());
    }
}

#[test]
fn reverse_constant_and_shortfall_reject_whole_file() {
    let mut bad = record(10);
    put32(&mut bad, 72, 999);
    let mut file = record(11);
    file.extend(bad);
    assert!(matches!(
        parse(&file),
        Err(ParseError::ReverseIntegrationMismatch { offset: 256, .. })
    ));
    let mut bad = record(10);
    put16(&mut bad, 30, 5);
    assert!(matches!(
        parse(&bad),
        Err(ParseError::SampleCountShortfall { declared: 5, .. })
    ));
}

#[test]
fn surplus_is_counted_and_invalid_steim2_dnib_fails() {
    let mut b = record(10);
    put16(&mut b, 30, 3);
    put32(&mut b, 72, 99);
    let r = parse(&b).unwrap();
    assert_eq!(r[0].samples, [100, 102, 99]);
    assert_eq!(r[0].steim_surplus_diffs, 1);
    for (code, dnib) in [(2, 0), (3, 3)] {
        let mut b = record(11);
        put32(&mut b, 64, code << 24);
        put32(&mut b, 76, dnib << 30);
        assert!(parse(&b).is_err());
    }
}

#[test]
fn format_limits_and_blockette_chains_are_explicit_errors() {
    for code in [0, 1, 3, 4, 5, 255] {
        let mut b = record(10);
        b[52] = code;
        assert!(matches!(parse(&b),Err(ParseError::UnsupportedEncoding{code:c,..}) if c==code));
    }
    let mut b = record(10);
    b[53] = 0;
    assert!(matches!(
        parse(&b),
        Err(ParseError::UnsupportedWordOrder { code: 0, .. })
    ));
    let mut b = record(10);
    put16(&mut b, 46, 56);
    b[39] = 1;
    assert!(matches!(
        parse(&b),
        Err(ParseError::MissingBlockette1000 { .. })
    ));
    for next in [48, 44, 300] {
        let mut b = record(10);
        put16(&mut b, 50, next);
        assert!(matches!(
            parse(&b),
            Err(ParseError::BadBlocketteChain { .. })
        ));
    }
}

#[test]
fn unknown_blockette_is_retained_byte_for_byte() {
    let mut b = record(10);
    put16(&mut b, 56, 2000);
    b[60..64].copy_from_slice(&[9, 8, 7, 6]);
    let r = parse(&b).unwrap();
    let unknown = r[0].blockettes.iter().find(|b| b.kind == 2000).unwrap();
    assert_eq!(unknown.offset, 56);
    assert_eq!(unknown.raw, b[56..64]);
}

#[test]
fn empty_truncated_and_mixed_record_lengths() {
    assert!(matches!(parse(&[]), Err(ParseError::EmptyInput)));
    assert!(parse(&record(10)[..100]).is_err());
    let mut b = record(10);
    b.extend_from_slice(&record(11)[..100]);
    assert!(matches!(
        parse(&b),
        Err(ParseError::TrailingBytes {
            offset: 256,
            len: 100
        })
    ));
    let mut b = record(10);
    b[54] = 9;
    b.resize(512, 0);
    b.extend(record(11));
    let r = parse(&b).unwrap();
    assert_eq!(r.len(), 2);
    assert_eq!(r[1].offset, 512);
    assert_eq!(r[1].record_length, 256);
}

#[test]
fn calendar_and_sample_rate_validation() {
    for (offset, value) in [(22, 0), (22, 367), (28, 10000)] {
        let mut b = record(10);
        put16(&mut b, offset, value);
        assert!(matches!(parse(&b), Err(ParseError::BadStartTime { .. })));
    }
    for (offset, value) in [(24, 24), (25, 60), (26, 60)] {
        let mut b = record(10);
        b[offset] = value;
        assert!(matches!(parse(&b), Err(ParseError::BadStartTime { .. })));
    }
    for offset in [32, 34] {
        let mut b = record(10);
        put16(&mut b, offset, 0);
        assert!(matches!(parse(&b), Err(ParseError::BadSampleRate { .. })));
    }
    let mut b = record(10);
    put16(&mut b, 32, (-10_i16) as u16);
    assert_eq!(parse(&b).unwrap()[0].sample_rate_hz(), Some(0.1));
}

#[test]
fn seed_corrections_are_applied_exactly_once_and_raw_values_retained() {
    let base = parse(&record(10)).unwrap()[0].start_unix_us;
    assert_eq!(base % 1_000_000, 123400);
    for applied in [false, true] {
        let mut b = record(10);
        put32(&mut b, 40, 100);
        b[61] = (-30_i8) as u8;
        if applied {
            b[36] = 2;
        }
        let r = parse(&b).unwrap();
        assert_eq!(
            r[0].start_unix_us,
            base - 30 + if applied { 0 } else { 10_000 }
        );
        assert_eq!(r[0].time_correction_ticks, 100);
        assert_eq!(r[0].microsecond_offset, Some(-30));
        assert_eq!(r[0].start.tenth_ms, 1234);
    }
}

#[test]
fn invalid_data_header_identifiers_are_rejected_without_lossy_replacement() {
    for (offset, value) in [(6, b'V'), (0, 0xff), (8, 0xff), (18, 0xff)] {
        let mut b = record(10);
        b[offset] = value;
        assert!(
            parse(&b).is_err(),
            "accepted invalid header byte at {offset}"
        );
    }
}

#[test]
fn declared_blockette_count_and_unique_encoding_declaration_are_enforced() {
    for count in [0, 1, 3] {
        let mut b = record(10);
        b[39] = count;
        assert!(
            parse(&b).is_err(),
            "accepted declared count {count} for two blockettes"
        );
    }
    let mut b = record(10);
    let duplicate = b[48..56].to_vec();
    b[56..64].copy_from_slice(&duplicate);
    put16(&mut b, 58, 0);
    assert!(
        parse(&b).is_err(),
        "duplicate blockette 1000 has ambiguous encoding authority"
    );
}

#[test]
fn unaligned_steim_frame_payload_is_not_silently_truncated() {
    let original = record(10);
    for begin in [65, 68] {
        let mut b = original.clone();
        b[64..].fill(0);
        b[begin..begin + 64].copy_from_slice(&original[64..128]);
        put16(&mut b, 44, begin as u16);
        assert!(
            parse(&b).is_err(),
            "accepted non-frame-aligned payload at {begin}"
        );
    }
}

#[test]
fn unsupported_sample_rate_override_cannot_be_preserved_but_ignored() {
    let original = record(10);
    let mut b = original.clone();
    b[64..].fill(0);
    b[128..192].copy_from_slice(&original[64..128]);
    put16(&mut b, 44, 128);
    put16(&mut b, 56, 100);
    put16(&mut b, 58, 0);
    b[60..64].copy_from_slice(&40.0_f32.to_be_bytes()); // B100 overrides header 20 Hz.
    assert!(
        parse(&b).is_err(),
        "unsupported B100 must not produce false 20 Hz sample times"
    );
}

#[test]
fn unrepresentable_sample_span_is_rejected_during_parse() {
    let mut b = record(11);
    b.resize(16_384, 0);
    b[54] = 14;
    b[63] = 0;
    put16(&mut b, 30, 10_000);
    put16(&mut b, 32, i16::MIN as u16);
    put16(&mut b, 34, i16::MIN as u16);
    b[64..].fill(0);
    for frame in (64..b.len()).step_by(64) {
        let first_word = if frame == 64 { 3 } else { 1 };
        let mut control = 0;
        for word in first_word..16 {
            control |= 3 << (30 - 2 * word);
            put32(&mut b, frame + word * 4, 2 << 30); // Seven zero 4-bit differences.
        }
        put32(&mut b, frame, control);
    }
    // Period is 1_073_741_824_000_000 us, so the declared span exceeds i64.
    // The structural parser must reject before callers derive sample times.
    assert!(
        parse(&b).is_err(),
        "accepted sample span outside representable time range"
    );
}
