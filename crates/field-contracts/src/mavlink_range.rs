use crate::{
    ArtifactReferenceV1, ArtifactRoleV1, ClockBasisV1, DigestV1, MissingValueV1, NumericRangeV1,
    ObservationExtensionV1, ObservationKindV1, ObservationValueV1, PrimitiveValueKindV1,
    QualityFlagV1, QualityStatusV1, QualityV1, SourceReferenceV1, Validate,
};

pub fn decode_mavlink_distance(
    frame: &[u8],
    source_id: &str,
) -> Result<Option<ObservationExtensionV1>, String> {
    if source_id.trim().is_empty() {
        return Err("source_id is required".into());
    }
    if frame.len() < 12 || frame[0] != 0xfd || frame[2] != 0 {
        return Err("expected unsigned MAVLink2 frame with supported flags".into());
    }
    let len = usize::from(frame[1]);
    if len == 0 || frame.len() != len + 12 {
        return Err("ambiguous or truncated MAVLink2 frame length".into());
    }
    if frame[7..10] != [132, 0, 0] {
        return Ok(None);
    }
    if len > 39 {
        return Err("DISTANCE_SENSOR payload exceeds 39 bytes".into());
    }
    let mut crc = 0xffff_u16;
    for &byte in frame[1..10 + len].iter().chain(std::iter::once(&85)) {
        let mut tmp = u16::from(byte) ^ (crc & 0xff);
        tmp ^= (tmp << 4) & 0xff;
        crc = (crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4);
    }
    if crc != u16::from_le_bytes([frame[10 + len], frame[11 + len]]) {
        return Err("DISTANCE_SENSOR CRC mismatch".into());
    }
    let mut payload = [0_u8; 39];
    payload[..len].copy_from_slice(&frame[10..10 + len]);
    let minimum = u16::from_le_bytes([payload[4], payload[5]]);
    let maximum = u16::from_le_bytes([payload[6], payload[7]]);
    let current = u16::from_le_bytes([payload[8], payload[9]]);
    let signal_quality = payload[38];
    if minimum > maximum || maximum == 0 || signal_quality > 100 {
        return Err("invalid range bounds or signal quality".into());
    }
    let mut flags = vec![QualityFlagV1::CalibrationUnknown];
    let (value, missing, mut status) = if signal_quality == 1 {
        (
            None,
            Some(MissingValueV1::SensorUnavailable),
            QualityStatusV1::Bad,
        )
    } else {
        (
            Some(ObservationValueV1::U64(u64::from(current))),
            None,
            if signal_quality == 0 {
                QualityStatusV1::Unknown
            } else {
                QualityStatusV1::Suspect
            },
        )
    };
    if value.is_some() && (current < minimum || current > maximum) {
        flags.push(QualityFlagV1::OutOfDeclaredRange);
        status = QualityStatusV1::Suspect;
    }
    flags.sort_unstable();
    let observation = ObservationExtensionV1 {
        schema_version: "1".into(),
        namespace: "mavlink".into(),
        semantic_id: "distance_sensor.current_distance".into(),
        semantic_version: "1".into(),
        event_time_ns: i64::from(u32::from_le_bytes([
            payload[0], payload[1], payload[2], payload[3],
        ])) * 1_000_000,
        clock_basis: ClockBasisV1::BootRelative,
        source: SourceReferenceV1 {
            source_id: format!("{source_id}/sys-{}/comp-{}", frame[5], frame[6]),
            sensor_location: format!(
                "rangefinder-{}/orientation-{}/type-{}",
                payload[11], payload[12], payload[10]
            ),
        },
        observation_kind: ObservationKindV1::Raw,
        value_kind: PrimitiveValueKindV1::U64,
        value,
        missing,
        unit: Some("cm".into()),
        resolution: Some(1.0),
        valid_range: Some(NumericRangeV1 {
            minimum: f64::from(minimum),
            maximum: f64::from(maximum),
        }),
        quality: QualityV1 {
            status,
            score: None,
            flags,
        },
        raw_artifacts: vec![ArtifactReferenceV1 {
            digest: DigestV1::sha256(frame),
            media_type: "application/vnd.mavlink".into(),
            role: ArtifactRoleV1::RawInput,
        }],
        transformer: None,
    };
    observation.validate().map_err(|err| err.to_string())?;
    Ok(Some(observation))
}
