#[test]
fn reported_rc_channels_preserve_all_values_and_count_boundaries() {
    let profile = parse_profile(PROFILE, "authored").unwrap();
    for (count, mav2) in [(4u8, false), (18, true), (0, true), (255, true)] {
        let mut payload = [0u8; 42];
        payload[..4].copy_from_slice(&1234u32.to_le_bytes());
        for channel in 1..=18 {
            let raw = if channel == 2 {
                u16::MAX
            } else if channel == 1 {
                0
            } else {
                1000 + channel as u16
            };
            payload[4 + (channel - 1) * 2..6 + (channel - 1) * 2]
                .copy_from_slice(&raw.to_le_bytes());
        }
        payload[40] = count;
        payload[41] = 255;
        let input = framed(65, &payload, mav2);
        let original = TlogReader.read(&profile, &input).unwrap();
        let expanded = TlogReader
            .read_with_reported_rc_channels(&profile, &input)
            .unwrap();
        assert_eq!(expanded.len(), 1);
        let row = &expanded[0];
        assert_eq!(row.t_boot_us, Some(1_234_000));
        assert_eq!(row.t_ms, original[0].t_ms);
        assert_eq!(row.clock_basis, original[0].clock_basis);
        for (key, value) in &original[0].fields {
            assert_eq!(crate::field(row, key), Some(value));
        }
        for channel in 1..=18 {
            assert!(crate::field(row, &format!("RC_CHANNELS.channel{channel}_raw")).is_some());
            assert_eq!(
                crate::field(row, &format!("RC_CHANNELS.channel{channel}_duration_us")).is_some(),
                count != 0 && channel != 2
            );
        }
        assert_eq!(
            crate::field(row, "RC_CHANNELS.channel2_basis"),
            Some(&FieldValue::Text("UNUSED_UINT16_MAX".into()))
        );
        assert_eq!(
            crate::field(row, "RC_CHANNELS.rssi_basis"),
            Some(&FieldValue::Text("UNAVAILABLE_UINT8_MAX".into()))
        );
        assert!(crate::field(&original[0], "RC_CHANNELS.channel18_raw").is_none());
    }
}

#[test]
fn reported_rc_channels_separate_input_and_crc_refusal() {
    let profile = parse_profile(PROFILE, "authored").unwrap();
    let mut payload = [0u8; 42];
    payload[4..6].copy_from_slice(&65534u16.to_le_bytes());
    payload[40] = 1;
    payload[41] = 254;
    let mut input = framed(65, &payload, true);
    let rows = TlogReader
        .read_with_reported_rc_channels(&profile, &input)
        .unwrap();
    assert_eq!(
        crate::field(&rows[0], "RC_CHANNELS.channel1_duration_us"),
        Some(&FieldValue::I64(65534))
    );
    assert_eq!(
        crate::field(&rows[0], "RC_CHANNELS.rssi_basis"),
        Some(&FieldValue::Text("DEVICE_DEPENDENT_RAW_SCALE".into()))
    );
    input[20] ^= 1;
    assert!(
        TlogReader
            .read_with_reported_rc_channels(&profile, &input)
            .is_err()
    );
}
