//! Read FDSN miniSEED 2.x data records without guessing anything the bytes do not state.
//!
//! Structural decoder written from the public SEED Reference Manual v2.4 (<https://doi.org/10.7914/en3h-2318>).
//! Scope is deliberately narrow and fails closed outside it: 48-byte big-endian fixed header, the
//! blockette chain, blockette 1000 (encoding, word order, record length) and blockette 1001 (timing
//! quality, microsecond offset), and Steim-1 / Steim-2 payloads. miniSEED 3, full SEED volumes,
//! int16/int32/float/ASCII encodings and little-endian records are rejected by name, never skipped.
//!
//! - Record length comes from blockette 1000 only; it is never assumed to be 512.
//! - Decoded samples are **instrument counts**. No instrument response, no SI conversion, no filtering.
//! - The start time is the source's own declared time: SEED BTIME plus header field 16 time correction
//!   (only when the activity flag says it has not been applied) plus the signed blockette 1001
//!   microsecond offset. The uncorrected value and both offsets stay available separately. Decoding a
//!   declared time is not a claim that the recorder's clock was synchronised.
//! - Sample periods that are not an exact whole number of microseconds are rejected, not rounded.
//! - Parsing is atomic: any error returns `Err`, never a partial record list.
//! - No dependencies, no network, no writes.

// なぜ: 本 crate は wire 型（u16/i16/u8）を内部 i64 へ持ち上げる構造 decoder で、cast は境界検査済みの
// 変換そのものが目的。doc は英語だが技術語を全て backtick で囲む規律は採らない（workspace 既定に合わせる）。
#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_sign_loss,
    clippy::cast_precision_loss,
    clippy::doc_markdown,
    clippy::missing_const_for_fn,
    clippy::module_name_repetitions
)]

/// One blockette of the chain, known or not; the raw bytes are always kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Blockette {
    pub kind: u16,
    /// Offset of the blockette within its record.
    pub offset: usize,
    pub raw: Vec<u8>,
}

/// SEED BTIME exactly as written in the header (no arithmetic applied).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BTime {
    pub year: u16,
    pub day_of_year: u16,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    /// 0.0001 s ticks (0..=9999).
    pub tenth_ms: u16,
}

impl BTime {
    /// SEED header rendering, e.g. `2024-001T00:00:15.7450`.
    #[must_use]
    pub fn to_seed_string(self) -> String {
        format!(
            "{:04}-{:03}T{:02}:{:02}:{:02}.{:04}",
            self.year, self.day_of_year, self.hour, self.minute, self.second, self.tenth_ms
        )
    }
}

/// One decoded data record.
#[derive(Debug, Clone, PartialEq)]
pub struct Record {
    /// Byte offset of the record in the parsed input.
    pub offset: usize,
    /// From blockette 1000, not guessed.
    pub record_length: usize,
    /// Six ASCII characters, leading zeros kept.
    pub sequence: String,
    /// Data-quality indicator (`D`/`R`/`Q`/`M` in practice; whatever the byte says).
    pub quality: char,
    pub network: String,
    pub station: String,
    /// May be empty; an empty location is a real value, not a missing one.
    pub location: String,
    pub channel: String,
    /// Header BTIME, unmodified.
    pub start: BTime,
    /// `start` as Unix microseconds, with no correction applied.
    pub start_unix_us_raw: i64,
    /// Declared source start: `start_unix_us_raw` + time correction (when not already applied)
    /// + blockette 1001 microsecond offset.
    pub start_unix_us: i64,
    pub sample_count_declared: usize,
    pub sample_count_decoded: usize,
    pub rate_factor: i16,
    pub rate_multiplier: i16,
    /// Exact sample interval in microseconds; a non-representable interval is a parse error.
    pub sample_period_us: i64,
    pub activity_flags: u8,
    pub io_clock_flags: u8,
    pub data_quality_flags: u8,
    /// Header field 16, in 0.0001 s ticks, as written.
    pub time_correction_ticks: i32,
    /// Activity flag bit 1: the recorder says the correction is already in the header BTIME.
    pub time_correction_applied: bool,
    pub encoding: u8,
    pub word_order: u8,
    pub data_begin_offset: usize,
    /// Every blockette of the chain in chain order, known or not.
    pub blockettes: Vec<Blockette>,
    /// Bytes between the end of the last blockette and the data start.
    pub reserved_gap: Vec<u8>,
    /// Blockette 1001 field 3.
    pub timing_quality_percent: Option<u8>,
    /// Blockette 1001 field 4, signed microseconds, as written.
    pub microsecond_offset: Option<i8>,
    /// Instrument counts. No response applied.
    pub samples: Vec<i32>,
    /// Forward integration constant (first sample of the record).
    pub steim_x0: i32,
    /// Reverse integration constant (last sample of the record).
    pub steim_xn: i32,
    /// Differences present in the frames past the declared sample count (frame padding), counted.
    pub steim_surplus_diffs: usize,
}

impl Record {
    /// FDSN source id `NET.STA.LOC.CHA`; an empty location stays empty.
    #[must_use]
    pub fn source_id(&self) -> String {
        format!(
            "{}.{}.{}.{}",
            self.network, self.station, self.location, self.channel
        )
    }

    /// Derived from the two header integers, which stay authoritative.
    #[must_use]
    pub fn sample_rate_hz(&self) -> Option<f64> {
        let (num, den) = rate_ratio(self.rate_factor, self.rate_multiplier)?;
        Some(num as f64 / den as f64)
    }

    /// Declared span of the record: `sample_count_declared * sample_period_us`.
    #[must_use]
    pub fn nominal_span_us(&self) -> i64 {
        self.sample_period_us * self.sample_count_declared as i64
    }

    /// Source time of sample `index`, on the same axis as [`Record::start_unix_us`].
    #[must_use]
    pub fn sample_time_unix_us(&self, index: usize) -> i64 {
        self.start_unix_us + self.sample_period_us * index as i64
    }

    /// Encoding label for the two supported codes.
    #[must_use]
    pub fn encoding_name(&self) -> &'static str {
        match self.encoding {
            STEIM1 => "steim1",
            STEIM2 => "steim2",
            _ => "unsupported",
        }
    }

    /// Blockette 1001 timing quality and the clock-locked I/O flag are publisher assertions about a
    /// clock this decoder cannot verify; they are carried, never promoted.
    #[must_use]
    pub fn clock_locked_flag(&self) -> bool {
        self.io_clock_flags & 0x20 != 0
    }

    /// Data-quality flag bit 4: the record contains missing or padded data.
    #[must_use]
    pub fn missing_or_padded(&self) -> bool {
        self.data_quality_flags & 0x10 != 0
    }

    /// Data-quality flag bit 7: the recorder marks its own time tag as questionable.
    #[must_use]
    pub fn time_tag_questionable(&self) -> bool {
        self.data_quality_flags & 0x80 != 0
    }
}

/// Parse failures. Every one names a byte offset; none of them is a silently dropped record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    EmptyInput,
    /// Fewer bytes than a fixed header remain at `offset`.
    Truncated {
        offset: usize,
    },
    /// A record header was readable but the record it declares does not fit in the input.
    TrailingBytes {
        offset: usize,
        len: usize,
    },
    /// A fixed-header identifier byte is not the ASCII the format defines. Replacing it would put an
    /// invented identity on every observation of the record, so the file is rejected instead.
    BadHeaderIdentifier {
        offset: usize,
        field: &'static str,
    },
    MissingBlockette1000 {
        offset: usize,
    },
    BadBlocketteChain {
        offset: usize,
        what: &'static str,
    },
    /// A blockette that changes how the record must be decoded, which this decoder does not apply.
    /// Keeping it as an uninterpreted blockette would leave the decoded values silently wrong.
    UnsupportedBlockette {
        offset: usize,
        kind: u16,
        what: &'static str,
    },
    UnsupportedEncoding {
        offset: usize,
        code: u8,
    },
    UnsupportedWordOrder {
        offset: usize,
        code: u8,
    },
    /// A Steim nibble/sub-nibble combination that the format does not define.
    UnsupportedSteimNibble {
        offset: usize,
        code: u8,
        dnib: u8,
    },
    BadStartTime {
        offset: usize,
        what: &'static str,
    },
    BadSampleRate {
        offset: usize,
    },
    /// The declared rate is valid but its period is not a whole number of microseconds; rounding it
    /// would move every sample time, so the record is rejected instead.
    SamplePeriodNotRepresentable {
        offset: usize,
        rate_factor: i16,
        rate_multiplier: i16,
    },
    /// The declared samples span more microseconds than the time axis can hold, so no sample time
    /// could be derived without wrapping.
    SampleSpanNotRepresentable {
        offset: usize,
        sample_count_declared: usize,
        sample_period_us: i64,
    },
    /// The data area is not a whole number of 64-byte Steim frames; the remainder would be dropped.
    UnalignedFramePayload {
        offset: usize,
        data_bytes: usize,
    },
    SampleCountShortfall {
        offset: usize,
        declared: usize,
        decoded: usize,
    },
    ReverseIntegrationMismatch {
        offset: usize,
        expected: i32,
        actual: i32,
    },
    /// A record of a stream starts before the previous record of the same stream.
    NonMonotonicRecords {
        offset: usize,
        previous_start_unix_us: i64,
        start_unix_us: i64,
    },
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyInput => write!(f, "empty input"),
            Self::Truncated { offset } => write!(f, "truncated record header at {offset}"),
            Self::TrailingBytes { offset, len } => {
                write!(f, "{len} trailing bytes at {offset} are not a whole record")
            }
            Self::BadHeaderIdentifier { offset, field } => {
                write!(f, "record at {offset} has an unusable {field} identifier")
            }
            Self::MissingBlockette1000 { offset } => {
                write!(f, "record at {offset} has no blockette 1000")
            }
            Self::BadBlocketteChain { offset, what } => {
                write!(f, "bad blockette chain in record at {offset}: {what}")
            }
            Self::UnsupportedBlockette { offset, kind, what } => {
                write!(f, "record at {offset} carries blockette {kind}: {what}")
            }
            Self::UnsupportedEncoding { offset, code } => write!(
                f,
                "record at {offset} uses encoding {code}; only 10 (Steim-1) and 11 (Steim-2) are decoded"
            ),
            Self::UnsupportedWordOrder { offset, code } => {
                write!(
                    f,
                    "record at {offset} declares word order {code}; only 1 (big-endian) is decoded"
                )
            }
            Self::UnsupportedSteimNibble { offset, code, dnib } => write!(
                f,
                "record at {offset} uses undefined Steim nibble {code}/{dnib}"
            ),
            Self::BadStartTime { offset, what } => {
                write!(f, "record at {offset} has an unusable start time: {what}")
            }
            Self::BadSampleRate { offset } => {
                write!(f, "record at {offset} has a zero sample rate")
            }
            Self::SamplePeriodNotRepresentable {
                offset,
                rate_factor,
                rate_multiplier,
            } => write!(
                f,
                "record at {offset} declares rate {rate_factor}/{rate_multiplier}, whose period is not a whole microsecond"
            ),
            Self::SampleSpanNotRepresentable {
                offset,
                sample_count_declared,
                sample_period_us,
            } => write!(
                f,
                "record at {offset} declares {sample_count_declared} samples of {sample_period_us} us, which leaves the representable time range"
            ),
            Self::UnalignedFramePayload { offset, data_bytes } => write!(
                f,
                "record at {offset} has a {data_bytes}-byte data area, which is not a whole number of 64-byte Steim frames"
            ),
            Self::SampleCountShortfall {
                offset,
                declared,
                decoded,
            } => write!(
                f,
                "record at {offset} declares {declared} samples but the frames carry {decoded}"
            ),
            Self::ReverseIntegrationMismatch {
                offset,
                expected,
                actual,
            } => write!(
                f,
                "record at {offset} ends at {actual}, but its reverse integration constant is {expected}"
            ),
            Self::NonMonotonicRecords {
                offset,
                previous_start_unix_us,
                start_unix_us,
            } => write!(
                f,
                "record at {offset} starts at {start_unix_us} us, before {previous_start_unix_us} us in the same stream"
            ),
        }
    }
}

impl std::error::Error for ParseError {}

impl ParseError {
    /// Byte offset the failure refers to (for `ReadError::Malformed`-style reporting).
    #[must_use]
    pub fn offset(&self) -> usize {
        match self {
            Self::EmptyInput => 0,
            Self::Truncated { offset }
            | Self::TrailingBytes { offset, .. }
            | Self::BadHeaderIdentifier { offset, .. }
            | Self::MissingBlockette1000 { offset }
            | Self::BadBlocketteChain { offset, .. }
            | Self::UnsupportedBlockette { offset, .. }
            | Self::UnalignedFramePayload { offset, .. }
            | Self::SampleSpanNotRepresentable { offset, .. }
            | Self::UnsupportedEncoding { offset, .. }
            | Self::UnsupportedWordOrder { offset, .. }
            | Self::UnsupportedSteimNibble { offset, .. }
            | Self::BadStartTime { offset, .. }
            | Self::BadSampleRate { offset }
            | Self::SamplePeriodNotRepresentable { offset, .. }
            | Self::SampleCountShortfall { offset, .. }
            | Self::ReverseIntegrationMismatch { offset, .. }
            | Self::NonMonotonicRecords { offset, .. } => *offset,
        }
    }
}

const FIXED_HEADER: usize = 48;
const FRAME: usize = 64;
const STEIM1: u8 = 10;
const STEIM2: u8 = 11;

/// Decode every data record in `bytes`.
///
/// # Errors
/// Any structural problem returns [`ParseError`]; a partially decoded file is never returned.
pub fn parse(bytes: &[u8]) -> Result<Vec<Record>, ParseError> {
    if bytes.is_empty() {
        return Err(ParseError::EmptyInput);
    }
    let mut records: Vec<Record> = Vec::new();
    let mut offset = 0usize;
    while offset < bytes.len() {
        let record = parse_record(bytes, offset)?;
        // Records of one stream must not go backwards in time; equal starts are kept (they are a
        // real recorded condition), decreasing starts fail closed.
        if let Some(previous) = records
            .iter()
            .rev()
            .find(|r| r.source_id() == record.source_id())
            && record.start_unix_us < previous.start_unix_us
        {
            return Err(ParseError::NonMonotonicRecords {
                offset,
                previous_start_unix_us: previous.start_unix_us,
                start_unix_us: record.start_unix_us,
            });
        }
        offset += record.record_length;
        records.push(record);
    }
    Ok(records)
}

fn parse_record(bytes: &[u8], offset: usize) -> Result<Record, ParseError> {
    let available = &bytes[offset..];
    if available.len() < FIXED_HEADER {
        return Err(ParseError::Truncated { offset });
    }

    // Identifiers are checked before anything is built from them: `from_utf8_lossy` would turn a
    // non-ASCII byte into U+FFFD and put an invented station or network on every observation.
    let bad_id = |field: &'static str| ParseError::BadHeaderIdentifier { offset, field };
    if !available[0..6]
        .iter()
        .all(|b| b.is_ascii_digit() || *b == b' ')
    {
        return Err(bad_id("sequence number"));
    }
    if !matches!(available[6], b'D' | b'R' | b'Q' | b'M') {
        return Err(bad_id("data quality"));
    }
    for (range, field) in [
        (8..13, "station"),
        (13..15, "location"),
        (15..18, "channel"),
        (18..20, "network"),
    ] {
        if !available[range]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b' ')
        {
            return Err(bad_id(field));
        }
    }
    let sequence = ascii(&available[0..6]);
    let quality = available[6] as char;
    let station = ascii(&available[8..13]);
    let location = ascii(&available[13..15]);
    let channel = ascii(&available[15..18]);
    let network = ascii(&available[18..20]);
    let start = BTime {
        year: be16(available, 20),
        day_of_year: be16(available, 22),
        hour: available[24],
        minute: available[25],
        second: available[26],
        tenth_ms: be16(available, 28),
    };
    let sample_count_declared = be16(available, 30) as usize;
    let rate_factor = be16(available, 32) as i16;
    let rate_multiplier = be16(available, 34) as i16;
    let activity_flags = available[36];
    let io_clock_flags = available[37];
    let data_quality_flags = available[38];
    let blockette_count_declared = available[39] as usize;
    let time_correction_ticks = be32(available, 40) as i32;
    let data_begin_offset = be16(available, 44) as usize;
    let first_blockette = be16(available, 46) as usize;

    // Blockette chain first: the record length itself is only knowable from blockette 1000.
    let (blockettes, record_length) =
        read_chain(available, offset, first_blockette, data_begin_offset)?;
    if available.len() < record_length {
        return Err(ParseError::TrailingBytes {
            offset,
            len: available.len(),
        });
    }
    let record = &available[..record_length];
    for b in &blockettes {
        if b.offset + b.raw.len() > record_length {
            return Err(ParseError::BadBlocketteChain {
                offset,
                what: "blockette offset outside record",
            });
        }
    }
    if data_begin_offset > record_length
        || (sample_count_declared > 0 && data_begin_offset < FIXED_HEADER)
    {
        return Err(ParseError::BadBlocketteChain {
            offset,
            what: "data begin offset outside record",
        });
    }
    // The header says how many blockettes follow. A chain that does not match it means one of the
    // two statements is wrong, and neither can be preferred silently.
    if blockettes.len() != blockette_count_declared {
        return Err(ParseError::BadBlocketteChain {
            offset,
            what: "header blockette count does not match the chain",
        });
    }
    // The declared data area must start after the chain: a blockette reaching into it means the two
    // bounds contradict each other, and either reading is a guess.
    if data_begin_offset >= FIXED_HEADER
        && blockettes
            .iter()
            .any(|b| b.offset + b.raw.len() > data_begin_offset)
    {
        return Err(ParseError::BadBlocketteChain {
            offset,
            what: "a blockette reaches into the declared data area",
        });
    }
    for kind in [1000u16, 1001] {
        if blockettes.iter().filter(|b| b.kind == kind).count() > 1 {
            return Err(ParseError::BadBlocketteChain {
                offset,
                what: "a decoding blockette is declared more than once",
            });
        }
    }
    // Blockette 100 restates the actual sample rate and overrides the header rate. This decoder uses
    // the header's exact rational rate, so accepting the record would publish sample times the
    // record itself contradicts.
    if let Some(b) = blockettes.iter().find(|b| b.kind == 100) {
        return Err(ParseError::UnsupportedBlockette {
            offset,
            kind: b.kind,
            what: "an actual sample rate that overrides the header rate this decoder uses",
        });
    }

    let b1000 = blockettes
        .iter()
        .find(|b| b.kind == 1000)
        .ok_or(ParseError::MissingBlockette1000 { offset })?;
    let encoding = b1000.raw[4];
    let word_order = b1000.raw[5];
    if word_order != 1 {
        return Err(ParseError::UnsupportedWordOrder {
            offset,
            code: word_order,
        });
    }
    if encoding != STEIM1 && encoding != STEIM2 {
        return Err(ParseError::UnsupportedEncoding {
            offset,
            code: encoding,
        });
    }

    let b1001 = blockettes.iter().find(|b| b.kind == 1001);
    let timing_quality_percent = b1001.map(|b| b.raw[4]);
    let microsecond_offset = b1001.map(|b| b.raw[5] as i8);

    let start_unix_us_raw = btime_unix_us(start, offset)?;
    let time_correction_applied = activity_flags & 0x02 != 0;
    let start_unix_us = start_unix_us_raw
        + if time_correction_applied {
            0
        } else {
            i64::from(time_correction_ticks) * 100
        }
        + i64::from(microsecond_offset.unwrap_or(0));

    let sample_period_us = sample_period_us(rate_factor, rate_multiplier, offset)?;
    // Sample times are derived as start + index * period. If the declared span cannot be held, the
    // record is rejected here rather than wrapping when a caller asks for a sample time.
    if sample_period_us
        .checked_mul(sample_count_declared as i64)
        .and_then(|span| start_unix_us.checked_add(span))
        .is_none()
    {
        return Err(ParseError::SampleSpanNotRepresentable {
            offset,
            sample_count_declared,
            sample_period_us,
        });
    }

    let reserved_gap = blockettes
        .last()
        .map(|b| b.offset + b.raw.len())
        .filter(|end| *end < data_begin_offset && data_begin_offset <= record_length)
        .map_or_else(Vec::new, |end| record[end..data_begin_offset].to_vec());

    // A record that declares no data area (blockette-only record) is decoded as zero frames rather
    // than by reading its own header as frames.
    let data: &[u8] = if (FIXED_HEADER..=record_length).contains(&data_begin_offset) {
        &record[data_begin_offset..]
    } else {
        &[]
    };
    // Steim data is a whole number of 64-byte frames. A remainder would be dropped by frame-wise
    // decoding, which is exactly the silent truncation this decoder must not do.
    if !data.len().is_multiple_of(FRAME) {
        return Err(ParseError::UnalignedFramePayload {
            offset,
            data_bytes: data.len(),
        });
    }
    let (steim_x0, steim_xn, diffs) = decode_steim(data, encoding, offset)?;
    let (samples, steim_surplus_diffs) = if sample_count_declared == 0 {
        (Vec::new(), diffs.len())
    } else {
        if diffs.len() < sample_count_declared {
            return Err(ParseError::SampleCountShortfall {
                offset,
                declared: sample_count_declared,
                decoded: diffs.len(),
            });
        }
        let mut samples = Vec::with_capacity(sample_count_declared);
        let mut value = steim_x0;
        samples.push(value);
        // The first encoded difference belongs to the previous record and is never applied here.
        for d in &diffs[1..sample_count_declared] {
            value = value.wrapping_add(*d);
            samples.push(value);
        }
        let last = *samples.last().unwrap_or(&steim_x0);
        if last != steim_xn {
            return Err(ParseError::ReverseIntegrationMismatch {
                offset,
                expected: steim_xn,
                actual: last,
            });
        }
        (samples, diffs.len() - sample_count_declared)
    };

    Ok(Record {
        offset,
        record_length,
        sequence,
        quality,
        network,
        station,
        location,
        channel,
        start,
        start_unix_us_raw,
        start_unix_us,
        sample_count_declared,
        sample_count_decoded: samples.len(),
        rate_factor,
        rate_multiplier,
        sample_period_us,
        activity_flags,
        io_clock_flags,
        data_quality_flags,
        time_correction_ticks,
        time_correction_applied,
        encoding,
        word_order,
        data_begin_offset,
        blockettes,
        reserved_gap,
        timing_quality_percent,
        microsecond_offset,
        samples,
        steim_x0,
        steim_xn,
        steim_surplus_diffs,
    })
}

/// Walk the blockette chain, keeping every blockette verbatim, and return the record length that
/// blockette 1000 declares. Bounds are checked against the bytes actually available.
fn read_chain(
    available: &[u8],
    offset: usize,
    first_blockette: usize,
    data_begin_offset: usize,
) -> Result<(Vec<Blockette>, usize), ParseError> {
    let bad = |what: &'static str| ParseError::BadBlocketteChain { offset, what };
    let mut blockettes: Vec<Blockette> = Vec::new();
    let mut record_length: Option<usize> = None;
    let mut next = first_blockette;
    let mut previous_end = FIXED_HEADER;
    while next != 0 {
        if next < previous_end {
            return Err(bad("blockette offset does not advance"));
        }
        if next + 4 > available.len() {
            return Err(bad("blockette offset outside record"));
        }
        let kind = be16(available, next);
        let following = be16(available, next + 2) as usize;
        if following != 0 && following <= next {
            return Err(bad("blockette offset does not advance"));
        }
        let end = if following != 0 {
            following
        } else if data_begin_offset > next {
            data_begin_offset
        } else {
            next + 4
        };
        if end > available.len() || end < next + 4 {
            return Err(bad("blockette extends outside record"));
        }
        if kind == 1000 {
            if end < next + 8 {
                return Err(bad("blockette 1000 is shorter than 8 bytes"));
            }
            let power = available[next + 6];
            if !(8..=24).contains(&power) {
                return Err(bad("blockette 1000 declares an unusable record length"));
            }
            record_length = Some(1usize << power);
        }
        if kind == 1001 && end < next + 8 {
            return Err(bad("blockette 1001 is shorter than 8 bytes"));
        }
        blockettes.push(Blockette {
            kind,
            offset: next,
            raw: available[next..end].to_vec(),
        });
        previous_end = end;
        next = following;
    }
    let record_length = record_length.ok_or(ParseError::MissingBlockette1000 { offset })?;
    Ok((blockettes, record_length))
}

/// Decode Steim-1/Steim-2 frames into (X0, Xn, differences). All frames in the data area are read,
/// so padding differences past the declared sample count can be counted rather than dropped.
fn decode_steim(
    data: &[u8],
    encoding: u8,
    offset: usize,
) -> Result<(i32, i32, Vec<i32>), ParseError> {
    let mut x0 = 0i32;
    let mut xn = 0i32;
    let mut diffs: Vec<i32> = Vec::new();
    for (frame_index, frame) in data.chunks_exact(FRAME).enumerate() {
        let control = be32(frame, 0);
        if frame_index == 0 {
            x0 = be32(frame, 4) as i32;
            xn = be32(frame, 8) as i32;
        }
        for word_index in 1..16usize {
            // Frame 0 words 1 and 2 are the integration constants, whatever their nibble says.
            if frame_index == 0 && (word_index == 1 || word_index == 2) {
                continue;
            }
            let code = ((control >> (30 - 2 * word_index)) & 0b11) as u8;
            if code == 0 {
                continue;
            }
            let word = be32(frame, 4 * word_index);
            let dnib = (word >> 30) as u8;
            let (width, count) = match (encoding, code) {
                (_, 1) => (8u32, 4usize),
                (STEIM1, 2) => (16, 2),
                (STEIM1, 3) => (32, 1),
                (STEIM2, 2) => match dnib {
                    1 => (30, 1),
                    2 => (15, 2),
                    3 => (10, 3),
                    _ => return Err(ParseError::UnsupportedSteimNibble { offset, code, dnib }),
                },
                (STEIM2, 3) => match dnib {
                    0 => (6, 5),
                    1 => (5, 6),
                    2 => (4, 7),
                    _ => return Err(ParseError::UnsupportedSteimNibble { offset, code, dnib }),
                },
                _ => return Err(ParseError::UnsupportedSteimNibble { offset, code, dnib }),
            };
            for i in 0..count {
                let shift = (count - 1 - i) as u32 * width;
                diffs.push(sign_extend(word >> shift, width));
            }
        }
    }
    Ok((x0, xn, diffs))
}

fn sign_extend(bits: u32, width: u32) -> i32 {
    let shift = 32 - width;
    ((bits << shift) as i32) >> shift
}

/// SEED sample rate as an exact rational (samples per second) from the two header integers.
fn rate_ratio(factor: i16, multiplier: i16) -> Option<(i64, i64)> {
    let (f, m) = (i64::from(factor), i64::from(multiplier));
    if f == 0 || m == 0 {
        return None;
    }
    Some(match (f > 0, m > 0) {
        (true, true) => (f * m, 1),
        (true, false) => (f, -m),
        (false, true) => (m, -f),
        (false, false) => (1, f * m),
    })
}

fn sample_period_us(factor: i16, multiplier: i16, offset: usize) -> Result<i64, ParseError> {
    let (num, den) = rate_ratio(factor, multiplier).ok_or(ParseError::BadSampleRate { offset })?;
    let scaled = 1_000_000i64 * den;
    if scaled % num != 0 {
        // Rounding here would silently move every sample time in the record.
        return Err(ParseError::SamplePeriodNotRepresentable {
            offset,
            rate_factor: factor,
            rate_multiplier: multiplier,
        });
    }
    Ok(scaled / num)
}

fn btime_unix_us(t: BTime, offset: usize) -> Result<i64, ParseError> {
    let bad = |what: &'static str| ParseError::BadStartTime { offset, what };
    if !(1900..=2100).contains(&t.year) {
        return Err(bad("year outside 1900..=2100"));
    }
    let days_in_year = if is_leap(i64::from(t.year)) { 366 } else { 365 };
    if t.day_of_year == 0 || i64::from(t.day_of_year) > days_in_year {
        return Err(bad("day of year outside the year"));
    }
    if t.hour > 23 {
        return Err(bad("hour above 23"));
    }
    if t.minute > 59 {
        return Err(bad("minute above 59"));
    }
    if t.second > 59 {
        // A leap second is not convertible to a Unix instant unambiguously; shifting it silently
        // would move the record, so it is rejected instead.
        return Err(bad("second above 59 (leap second not convertible)"));
    }
    if t.tenth_ms > 9999 {
        return Err(bad("0.0001 s ticks above 9999"));
    }
    let days = days_from_civil(i64::from(t.year), 1, 1) + i64::from(t.day_of_year) - 1;
    let seconds =
        days * 86_400 + i64::from(t.hour) * 3600 + i64::from(t.minute) * 60 + i64::from(t.second);
    Ok(seconds * 1_000_000 + i64::from(t.tenth_ms) * 100)
}

const fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// Days from 1970-01-01 (proleptic Gregorian, 86400-second days, no leap-second table).
const fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Header text field: ASCII with SEED's trailing-space padding removed, other bytes kept visible.
fn ascii(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).trim_end().to_string()
}

fn be16(bytes: &[u8], at: usize) -> u16 {
    u16::from_be_bytes([bytes[at], bytes[at + 1]])
}

fn be32(bytes: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]])
}
