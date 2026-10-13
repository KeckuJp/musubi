//! Independent authored wire cases for a saved navigation report, not route ground truth.
use musubi_reference_readers::{
    FieldValue, ProfileReader, field, profile::parse_profile, tlog::TlogReader,
};

fn frame(payload: &[u8], v2: bool, id: u8, extra: u8) -> Vec<u8> {
    let mut wire = if v2 {
        vec![0xfd, payload.len() as u8, 0, 0, 8, 9, 10, id, 0, 0]
    } else {
        vec![0xfe, payload.len() as u8, 8, 9, 10, id]
    };
    wire.extend_from_slice(payload);
    let mut crc = 0xffffu16;
    for byte in wire[1..].iter().chain(std::iter::once(&extra)) {
        crc ^= u16::from(*byte);
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ 0x8408
            } else {
                crc >> 1
            };
        }
    }
    wire.extend_from_slice(&crc.to_le_bytes());
    let mut recorded = 1_234_567u64.to_be_bytes().to_vec();
    recorded.extend_from_slice(&wire);
    recorded
}

fn payload() -> Vec<u8> {
    let mut p = Vec::new();
    for value in [-1.5f32, 2.25, -3.5, 4.25, -5.5] {
        p.extend_from_slice(&value.to_le_bytes());
    }
    p.extend_from_slice(&(-179i16).to_le_bytes());
    p.extend_from_slice(&180i16.to_le_bytes());
    p.extend_from_slice(&65535u16.to_le_bytes());
    p
}

fn profile() -> musubi_reference_types::FamilyProfile {
    parse_profile(
        include_str!("../../../profiles/declared/tlog-state/gcs-recorded-profile.toml"),
        "authored",
    )
    .unwrap()
}

#[test]
fn saved_nav_report_preserves_signed_quantities_and_not_a_device_clock() {
    for v2 in [false, true] {
        let mut bytes = frame(&payload(), v2, 62, 183);
        bytes.extend(frame(&[7], v2, 200, 0)); // unknown stays as its own record
        let rows = TlogReader.read(&profile(), &bytes).unwrap();
        assert_eq!(rows.len(), 2);
        let row = &rows[0];
        for (name, value) in [
            ("desired_roll_deg_reported", -1.5),
            ("desired_pitch_deg_reported", 2.25),
            ("altitude_error_m_reported", -3.5),
            ("airspeed_error_m_s_reported", 4.25),
            ("xtrack_error_m_reported", -5.5),
        ] {
            assert_eq!(
                field(row, &format!("NAV_CONTROLLER_OUTPUT.{name}")),
                Some(&FieldValue::F64(value))
            );
        }
        assert_eq!(field(row, "MAVLINK.system_id"), Some(&FieldValue::I64(9)));
        assert_eq!(
            field(row, "MAVLINK.component_id"),
            Some(&FieldValue::I64(10))
        );
        assert!(row.anchor_unix_us.is_none());
        assert!(row.t_boot_us.is_none());
        assert_eq!(
            field(&rows[1], "MAVLINK.message_id"),
            Some(&FieldValue::I64(200))
        );
    }
}

#[test]
fn zero_tail_is_wire_semantics_not_missing_and_malformed_is_not_silently_skipped() {
    let full = TlogReader
        .read(&profile(), &frame(&[0; 26], false, 62, 183))
        .unwrap();
    let short = TlogReader
        .read(&profile(), &frame(&[0], true, 62, 183))
        .unwrap();
    for rows in [full, short] {
        assert_eq!(
            field(&rows[0], "NAV_CONTROLLER_OUTPUT.xtrack_error_m_reported"),
            Some(&FieldValue::F64(0.0))
        );
    }
    for p in [vec![0; 25], vec![0; 27]] {
        assert!(
            TlogReader
                .read(&profile(), &frame(&p, false, 62, 183))
                .is_err()
        );
    }
    let mut bad = frame(&payload(), true, 62, 183);
    let tail = bad.len() - 1;
    bad[tail] ^= 1;
    assert!(TlogReader.read(&profile(), &bad).is_err());
    let mut truncated = frame(&payload(), true, 62, 183);
    truncated.pop();
    assert!(TlogReader.read(&profile(), &truncated).is_err());
}

#[test]
fn each_nonfinite_navigation_quantity_is_refused() {
    for offset in [0, 4, 8, 12, 16] {
        for value in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            let mut p = payload();
            p[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
            assert!(
                TlogReader
                    .read(&profile(), &frame(&p, true, 62, 183))
                    .is_err()
            );
        }
    }
}
