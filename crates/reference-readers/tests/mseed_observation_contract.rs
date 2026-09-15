use musubi_reference_readers::{
    FieldValue, ReadError,
    mseed_waveform::{MseedWaveformReader, field_units},
};
use musubi_reference_types::{ChannelId, ClockBasis, SourceRole};
#[path = "../../miniseed2/tests/support/mod.rs"]
pub mod support;
use support::{put16, put32, record};

fn field(o: &musubi_reference_readers::Observation, name: &str) -> FieldValue {
    o.fields
        .iter()
        .find(|(key, _)| key == name)
        .unwrap_or_else(|| panic!("missing field {name}"))
        .1
        .clone()
}

#[test]
fn each_sample_reaches_common_observation_with_exact_time_and_counts() {
    let b = record(11);
    let report = MseedWaveformReader.read_report(&b).unwrap();
    assert_eq!(report.records.len(), 1);
    assert_eq!(report.total_samples_decoded, 4);
    assert_eq!(report.observations.len(), 4);
    let start = report.records[0].start_unix_us;
    for (index, (o, value)) in report
        .observations
        .iter()
        .zip([100, 102, 99, 103])
        .enumerate()
    {
        assert_eq!(field(o, "sample_value_count"), FieldValue::I64(value));
        assert_eq!(field(o, "sample_index"), FieldValue::I64(index as i64));
        assert_eq!(
            field(o, "sample_time_unix_us"),
            FieldValue::I64(start + index as i64 * 50_000)
        );
        assert_eq!(field(o, "record_start_unix_us"), FieldValue::I64(start));
        assert_eq!(
            field(o, "record_sequence"),
            FieldValue::Text("000001".into())
        );
        assert_eq!(o.t_ms, ((start + index as i64 * 50_000) / 1000) as i64);
    }
    assert_eq!(field_units()["sample_value_count"], "count");
    for unit in field_units().values() {
        assert!(!unit.contains("m/s"));
    }
}

#[test]
fn source_clock_claims_never_become_verified_clock_or_platform() {
    let mut b = record(10);
    b[37] = 0x20;
    put32(&mut b, 40, 100);
    b[61] = (-30_i8) as u8;
    let report = MseedWaveformReader.read_report(&b).unwrap();
    for o in &report.observations {
        assert_eq!(o.clock_basis, ClockBasis::Unknown);
        assert_eq!(o.time_confidence, 0.0);
        assert_eq!(o.wall_ms, None);
        assert_eq!(o.anchor_unix_us, None);
        assert_eq!(o.t_boot_us, None);
        assert_eq!(o.source_role, SourceRole::RecordedExport);
        assert_eq!(o.channel, ChannelId::Onboard);
        assert_eq!(field(o, "clock_locked_flag"), FieldValue::I64(1));
        assert_eq!(field(o, "timing_quality_percent"), FieldValue::I64(100));
        assert_eq!(
            field(o, "fdsn_source_id"),
            FieldValue::Text("XX.SYN01..BHZ".into())
        );
        assert_eq!(field(o, "location"), FieldValue::Text(String::new()));
        assert_eq!(field(o, "time_correction_ticks"), FieldValue::I64(100));
        assert_eq!(field(o, "blockette1001_microsecond"), FieldValue::I64(-30));
    }
}

#[test]
fn second_stream_reuses_reader_and_preserves_input_order() {
    let mut b = record(11);
    let mut second = record(10);
    second[8..13].copy_from_slice(b"SYN02");
    second[37] = 0x20;
    b.extend(second);
    let report = MseedWaveformReader.read_report(&b).unwrap();
    assert_eq!(report.observations.len(), 8);
    assert_eq!(report.total_samples_decoded, 8);
    assert_eq!(report.streams, ["XX.SYN01..BHZ", "XX.SYN02..BHZ"]);
    for (index, o) in report.observations.iter().enumerate() {
        assert_eq!(
            field(o, "station"),
            FieldValue::Text(if index < 4 { "SYN01" } else { "SYN02" }.into())
        );
        assert_eq!(field(o, "gap_to_previous_us"), FieldValue::Blank);
    }
}

#[test]
fn gap_and_overlap_are_visible_without_sample_filling() {
    let mut b = record(10);
    let mut gap = record(10);
    gap[26] = 4;
    put16(&mut gap, 28, 3234);
    let mut overlap = record(10);
    overlap[26] = 4;
    put16(&mut overlap, 28, 4234);
    b.extend(gap);
    b.extend(overlap);
    let report = MseedWaveformReader.read_report(&b).unwrap();
    assert_eq!(report.observations.len(), 12);
    assert_eq!(
        field(&report.observations[4], "gap_to_previous_us"),
        FieldValue::I64(1_000_000)
    );
    assert_eq!(
        field(&report.observations[8], "gap_to_previous_us"),
        FieldValue::I64(-100_000)
    );
}

#[test]
fn missing_flag_stale_zero_sample_accounting_and_atomic_error() {
    let mut b = record(10);
    b[38] = 0x10;
    assert!(
        MseedWaveformReader
            .read(&b)
            .unwrap()
            .iter()
            .all(|o| o.stale)
    );
    put16(&mut b, 30, 0);
    let r = MseedWaveformReader.read_report(&b).unwrap();
    assert_eq!(r.records.len(), 1);
    assert_eq!(r.total_samples_decoded, 0);
    assert!(r.observations.is_empty());
    for bad in [
        vec![],
        b"time,value\n1,2\n".to_vec(),
        record(10)[..100].to_vec(),
    ] {
        assert!(matches!(
            MseedWaveformReader.read(&bad),
            Err(ReadError::Malformed { .. })
        ));
    }
}

#[test]
fn zero_data_offset_accounts_for_empty_payload_without_double_counting_header() {
    let mut b = record(10);
    put16(&mut b, 30, 0);
    put16(&mut b, 44, 0);
    put16(&mut b, 56, 2000);
    let report = MseedWaveformReader.read_report(&b).unwrap();
    assert_eq!(report.records.len(), 1);
    assert_eq!(report.total_samples_decoded, 0);
    assert!(report.observations.is_empty());
    let accounting = &report.accounting[0];
    assert_eq!(accounting.record_length_bytes, b.len());
    assert_eq!(accounting.data_begin_offset, 0);
    assert_eq!(accounting.data_bytes(), 0);
    assert_eq!(
        accounting.reserved_gap_bytes,
        b.len() - 48 - accounting.blockette_bytes
    );
    assert!(accounting.bytes_balanced());
}

#[test]
fn digest_covers_interior_samples_not_only_record_summary() {
    let b = record(10);
    let original = MseedWaveformReader.read(&b).unwrap();
    let again = MseedWaveformReader.read(&b).unwrap();
    assert_eq!(original[1].digest, again[1].digest);
    let mut altered = b;
    put32(&mut altered, 76, 0x4903fc04); // [100,103,99,103], same first/last/min/max
    let changed = MseedWaveformReader.read(&altered).unwrap();
    assert_ne!(original[1].digest, changed[1].digest);
}
