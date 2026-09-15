use crate::{ChannelId, ClockBasis, FieldValue, Observation, ReadError, SourceRole};
use musubi_miniseed2::{ParseError, Record};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy)]
pub struct MseedWaveformReader;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownBlockette {
    pub record_index: usize,
    pub record_offset: usize,
    pub offset: usize,
    pub kind: u16,
    pub raw: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RecordAccounting {
    pub record_index: usize,
    pub record_offset: usize,
    pub source_id: String,
    pub sequence: String,
    pub record_length_bytes: usize,
    pub sample_count_declared: usize,
    pub sample_count_decoded: usize,
    pub gap_to_previous_us: Option<i64>,
    pub blockette_types: Vec<u16>,
    pub blockette_bytes: usize,
    pub reserved_gap_bytes: usize,
    pub data_begin_offset: usize,
    pub unknown_blockette_types: Vec<u16>,
    pub unknown_blockette_bytes: usize,
    pub steim_surplus_diffs: usize,
    pub stale: bool,
}

impl RecordAccounting {
    #[must_use]
    pub fn bytes_balanced(&self) -> bool {
        48 + self.blockette_bytes + self.reserved_gap_bytes + self.data_bytes()
            == self.record_length_bytes
    }

    #[must_use]
    pub fn data_bytes(&self) -> usize {
        if self.data_begin_offset == 0 {
            return 0;
        }
        self.record_length_bytes
            .saturating_sub(self.data_begin_offset)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ReadReport {
    pub observations: Vec<Observation>,
    pub records: Vec<Record>,
    pub accounting: Vec<RecordAccounting>,
    pub total_samples_decoded: usize,
    pub streams: Vec<String>,
    pub unknown_blockettes: Vec<UnknownBlockette>,
    pub unknown_blockette_types: Vec<u16>,
    pub unknown_blockette_bytes: usize,
}

const KNOWN_BLOCKETTES: [u16; 2] = [1000, 1001];

impl MseedWaveformReader {
    pub fn read_report(&self, bytes: &[u8]) -> Result<ReadReport, ReadError> {
        let records = musubi_miniseed2::parse(bytes).map_err(malformed)?;

        let mut streams: Vec<String> = Vec::new();
        let mut accounting: Vec<RecordAccounting> = Vec::new();
        let mut unknown_blockettes: Vec<UnknownBlockette> = Vec::new();
        let mut observations: Vec<Observation> = Vec::new();
        let mut previous_end: BTreeMap<String, i64> = BTreeMap::new();

        for (record_index, record) in records.iter().enumerate() {
            let source_id = record.source_id();
            if !streams.contains(&source_id) {
                streams.push(source_id.clone());
            }
            let gap_to_previous_us = previous_end
                .get(&source_id)
                .map(|end| record.start_unix_us - *end);
            previous_end.insert(
                source_id.clone(),
                record.start_unix_us + record.nominal_span_us(),
            );

            for b in &record.blockettes {
                if !KNOWN_BLOCKETTES.contains(&b.kind) {
                    unknown_blockettes.push(UnknownBlockette {
                        record_index,
                        record_offset: record.offset,
                        offset: b.offset,
                        kind: b.kind,
                        raw: b.raw.clone(),
                    });
                }
            }
            let unknown: Vec<&musubi_miniseed2::Blockette> = record
                .blockettes
                .iter()
                .filter(|b| !KNOWN_BLOCKETTES.contains(&b.kind))
                .collect();
            let blockette_bytes: usize = record.blockettes.iter().map(|b| b.raw.len()).sum();

            accounting.push(RecordAccounting {
                record_index,
                record_offset: record.offset,
                source_id: source_id.clone(),
                sequence: record.sequence.clone(),
                record_length_bytes: record.record_length,
                sample_count_declared: record.sample_count_declared,
                sample_count_decoded: record.sample_count_decoded,
                gap_to_previous_us,
                blockette_types: record.blockettes.iter().map(|b| b.kind).collect(),
                blockette_bytes,
                reserved_gap_bytes: (if record.data_begin_offset == 0 {
                    record.record_length
                } else {
                    record.data_begin_offset
                })
                .saturating_sub(48 + blockette_bytes),
                data_begin_offset: record.data_begin_offset,
                unknown_blockette_types: unknown.iter().map(|b| b.kind).collect(),
                unknown_blockette_bytes: unknown.iter().map(|b| b.raw.len()).sum(),
                steim_surplus_diffs: record.steim_surplus_diffs,
                stale: record.missing_or_padded(),
            });

            let stale = record.missing_or_padded();
            for (sample_index, value) in record.samples.iter().enumerate() {
                let t_us = record.sample_time_unix_us(sample_index);
                let fields = sample_fields(
                    record,
                    record_index,
                    sample_index,
                    *value,
                    t_us,
                    gap_to_previous_us,
                );
                observations.push(observation(t_us, fields, stale));
            }
        }

        let mut unknown_blockette_types: Vec<u16> =
            unknown_blockettes.iter().map(|b| b.kind).collect();
        unknown_blockette_types.sort_unstable();
        unknown_blockette_types.dedup();
        let unknown_blockette_bytes = unknown_blockettes.iter().map(|b| b.raw.len()).sum();

        Ok(ReadReport {
            total_samples_decoded: observations.len(),
            observations,
            records,
            accounting,
            streams,
            unknown_blockettes,
            unknown_blockette_types,
            unknown_blockette_bytes,
        })
    }

    pub fn read(&self, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        Ok(self.read_report(bytes)?.observations)
    }
}

fn malformed(error: ParseError) -> ReadError {
    ReadError::Malformed {
        offset: error.offset(),
        what: error.to_string(),
    }
}

fn observation(t_us: i64, fields: Vec<(String, FieldValue)>, stale: bool) -> Observation {
    let t_ms = t_us.div_euclid(1000);
    Observation {
        t_ms,
        clock_basis: ClockBasis::Unknown,
        time_confidence: 0.0,
        source_role: SourceRole::RecordedExport,
        channel: ChannelId::Onboard,
        digest: crate::observation_digest(t_ms, ChannelId::Onboard, &fields),
        fields,
        stale,
        t_boot_us: None,
        anchor_unix_us: None,
        wall_ms: None,
    }
}

fn sample_fields(
    record: &Record,
    record_index: usize,
    sample_index: usize,
    value: i32,
    sample_time_unix_us: i64,
    gap_to_previous_us: Option<i64>,
) -> Vec<(String, FieldValue)> {
    let text = |s: &str| FieldValue::Text(s.to_string());
    let int = |v: i64| FieldValue::I64(v);
    let flag = |b: bool| FieldValue::I64(i64::from(b));
    vec![
        ("fdsn_source_id".into(), text(&record.source_id())),
        ("network".into(), text(&record.network)),
        ("station".into(), text(&record.station)),
        ("location".into(), text(&record.location)),
        ("channel_code".into(), text(&record.channel)),
        ("record_sequence".into(), text(&record.sequence)),
        (
            "data_quality_indicator".into(),
            text(&record.quality.to_string()),
        ),
        ("record_index".into(), int(record_index as i64)),
        ("record_start_unix_us".into(), int(record.start_unix_us)),
        (
            "record_start_unix_us_raw".into(),
            int(record.start_unix_us_raw),
        ),
        (
            "record_start_seed".into(),
            text(&record.start.to_seed_string()),
        ),
        ("sample_index".into(), int(sample_index as i64)),
        ("sample_value_count".into(), int(i64::from(value))),
        ("sample_time_unix_us".into(), int(sample_time_unix_us)),
        ("sample_period_us".into(), int(record.sample_period_us)),
        (
            "sample_rate_factor".into(),
            int(i64::from(record.rate_factor)),
        ),
        (
            "sample_rate_multiplier".into(),
            int(i64::from(record.rate_multiplier)),
        ),
        (
            "sample_count_declared".into(),
            int(record.sample_count_declared as i64),
        ),
        ("encoding_code".into(), int(i64::from(record.encoding))),
        ("encoding_name".into(), text(record.encoding_name())),
        (
            "record_length_bytes".into(),
            int(record.record_length as i64),
        ),
        (
            "activity_flags".into(),
            int(i64::from(record.activity_flags)),
        ),
        (
            "io_clock_flags".into(),
            int(i64::from(record.io_clock_flags)),
        ),
        (
            "data_quality_flags".into(),
            int(i64::from(record.data_quality_flags)),
        ),
        ("clock_locked_flag".into(), flag(record.clock_locked_flag())),
        (
            "time_tag_questionable_flag".into(),
            flag(record.time_tag_questionable()),
        ),
        (
            "timing_quality_percent".into(),
            record
                .timing_quality_percent
                .map_or(FieldValue::Blank, |q| int(i64::from(q))),
        ),
        (
            "blockette1001_microsecond".into(),
            record
                .microsecond_offset
                .map_or(FieldValue::Blank, |m| int(i64::from(m))),
        ),
        (
            "time_correction_ticks".into(),
            int(i64::from(record.time_correction_ticks)),
        ),
        (
            "time_correction_applied_flag".into(),
            flag(record.time_correction_applied),
        ),
        (
            "gap_to_previous_us".into(),
            gap_to_previous_us.map_or(FieldValue::Blank, int),
        ),
    ]
}

#[must_use]
pub fn field_units() -> BTreeMap<&'static str, &'static str> {
    BTreeMap::from([
        ("fdsn_source_id", "text"),
        ("network", "text"),
        ("station", "text"),
        ("location", "text"),
        ("channel_code", "text"),
        ("record_sequence", "text"),
        ("data_quality_indicator", "text"),
        ("record_index", "index"),
        ("record_start_unix_us", "us"),
        ("record_start_unix_us_raw", "us"),
        ("record_start_seed", "text"),
        ("sample_index", "index"),
        ("sample_value_count", "count"),
        ("sample_time_unix_us", "us"),
        ("sample_period_us", "us"),
        ("sample_rate_factor", "seed_rate_code"),
        ("sample_rate_multiplier", "seed_rate_code"),
        ("sample_count_declared", "count"),
        ("encoding_code", "seed_encoding_code"),
        ("encoding_name", "text"),
        ("record_length_bytes", "byte"),
        ("activity_flags", "bitfield"),
        ("io_clock_flags", "bitfield"),
        ("data_quality_flags", "bitfield"),
        ("clock_locked_flag", "flag"),
        ("time_tag_questionable_flag", "flag"),
        ("timing_quality_percent", "percent"),
        ("blockette1001_microsecond", "us"),
        ("time_correction_ticks", "tick_0.0001s"),
        ("time_correction_applied_flag", "flag"),
        ("gap_to_previous_us", "us"),
    ])
}
