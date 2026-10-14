//! Bounded LF framing for the existing decoded DataFlash GPS Observation path.
//!
//! No transport or external decoder: callers supply chunks and receive each event immediately.
//! Untimed/stateful metadata is explicitly unsupported, not inferred or silently dropped.

use musubi_reference_types::FamilyProfile;
use musubi_types::PlatformDomain;

use crate::{Observation, ReadError, mavlog_json::MavlogJsonReader};

/// Maximum retained bytes per frame, excluding LF but including a possible CR.
pub const MAX_FRAME_BYTES: usize = 65_536;

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct StreamCounts {
    pub bytes_received: u64,
    pub frames: u64,
    pub observations: u64,
    pub unsupported_frames: u64,
    pub invalid_frames: u64,
    pub oversized_frames: u64,
    pub incomplete_frames: u64,
}

#[derive(Debug)]
pub enum FrameResult {
    Observation(Observation),
    Unsupported(&'static str),
    Invalid(&'static str),
    Oversized,
    IncompleteEof,
}

#[derive(Debug)]
pub struct StreamEvent {
    pub source_id: String,
    /// Caller-supplied host arrival of the final chunk; never a producer UTC anchor.
    pub arrival_unix_ms: Option<i64>,
    pub frame_index: u64,
    /// Full frame bytes, or a labeled bounded prefix if the frame exceeded the limit.
    pub source_bytes: Vec<u8>,
    pub source_bytes_truncated: bool,
    pub frame_bytes: u64,
    pub terminated_by_lf: bool,
    pub platform_domain: PlatformDomain,
    pub result: FrameResult,
    /// Received bytes include the current supplied chunk; frames count finalized events.
    pub counts: StreamCounts,
}

pub struct GpsJsonlStream {
    profile: FamilyProfile,
    source_id: String,
    buffer: Vec<u8>,
    frame_bytes: u64,
    arrival_unix_ms: Option<i64>,
    counts: StreamCounts,
    finished: bool,
}

fn stream_error(what: &str) -> ReadError {
    ReadError::Malformed {
        offset: 0,
        what: what.into(),
    }
}

fn add(value: u64, delta: u64) -> Result<u64, ReadError> {
    value
        .checked_add(delta)
        .ok_or_else(|| stream_error("stream counter overflow"))
}

impl GpsJsonlStream {
    /// Units are declarations for the adopted decoded-GPS subset, never guessed from values.
    ///
    /// # Errors
    /// Rejects an empty/untrimmed/oversized source ID or incompatible profile declarations.
    pub fn new(profile: FamilyProfile, source_id: String) -> Result<Self, ReadError> {
        if source_id.is_empty() || source_id.len() > 128 || source_id.trim() != source_id {
            return Err(ReadError::Profile(
                "source ID must be 1..128 bytes, trimmed".into(),
            ));
        }
        if profile.format != "pymavlink_dataflash_jsonl" || profile.fields.time != "TimeUS" {
            return Err(ReadError::Profile(
                "expected decoded DataFlash TimeUS profile".into(),
            ));
        }
        for (field, unit) in [
            ("TimeUS", "us"),
            ("GPS.Lat", "deg"),
            ("GPS.Lng", "deg"),
            ("GPS.Alt", "m"),
        ] {
            if profile.field_units.get(field).map(String::as_str) != Some(unit) {
                return Err(ReadError::Profile(format!(
                    "expected declared {field} unit {unit}"
                )));
            }
        }
        Ok(Self {
            profile,
            source_id,
            buffer: Vec::new(),
            frame_bytes: 0,
            arrival_unix_ms: None,
            counts: StreamCounts::default(),
            finished: false,
        })
    }

    #[must_use]
    pub const fn counts(&self) -> &StreamCounts {
        &self.counts
    }

    #[must_use]
    pub fn buffered_bytes(&self) -> usize {
        self.buffer.len()
    }

    /// Process arbitrary fragments. LF finalizes a frame and invokes `emit` before returning.
    /// No stream history or total-session byte limit. A caller retaining events owns that memory.
    /// Overflow is terminal and explicit; malformed/unsupported frames are ordinary counted events.
    ///
    /// # Errors
    /// Rejects calls after finish and terminates on byte/frame/outcome counter overflow.
    pub fn push(
        &mut self,
        bytes: &[u8],
        arrival_unix_ms: Option<i64>,
        mut emit: impl FnMut(StreamEvent),
    ) -> Result<(), ReadError> {
        if self.finished {
            return Err(stream_error("stream already finished"));
        }
        let result = self.push_inner(bytes, arrival_unix_ms, &mut emit);
        if result.is_err() {
            self.finished = true;
        }
        result
    }

    fn push_inner(
        &mut self,
        bytes: &[u8],
        arrival_unix_ms: Option<i64>,
        emit: &mut impl FnMut(StreamEvent),
    ) -> Result<(), ReadError> {
        let received =
            u64::try_from(bytes.len()).map_err(|_| stream_error("input size overflow"))?;
        self.counts.bytes_received = add(self.counts.bytes_received, received)?;
        let mut remaining = bytes;
        while !remaining.is_empty() {
            let end = remaining.iter().position(|&b| b == b'\n');
            let length = end.unwrap_or(remaining.len());
            let segment = &remaining[..length];
            self.frame_bytes = add(
                self.frame_bytes,
                u64::try_from(length).map_err(|_| stream_error("frame size overflow"))?,
            )?;
            let retained = segment.len().min(MAX_FRAME_BYTES - self.buffer.len());
            self.buffer.extend_from_slice(&segment[..retained]);
            self.arrival_unix_ms = arrival_unix_ms;
            if let Some(end) = end {
                self.emit_frame(true, emit)?;
                remaining = &remaining[end + 1..];
            } else {
                break;
            }
        }
        Ok(())
    }

    /// EOF does not turn an unterminated tail into a valid frame, even if it is complete JSON.
    ///
    /// # Errors
    /// Rejects repeated finish calls and frame/outcome counter overflow.
    pub fn finish(&mut self, mut emit: impl FnMut(StreamEvent)) -> Result<(), ReadError> {
        if self.finished {
            return Err(stream_error("stream already finished"));
        }
        self.finished = true;
        if self.frame_bytes > 0 {
            self.emit_frame(false, &mut emit)?;
        }
        Ok(())
    }

    fn decode_frame(&self) -> FrameResult {
        let bytes = self.buffer.strip_suffix(b"\r").unwrap_or(&self.buffer);
        let Ok(record) = musubi_jsonl_log::parse_unique_json(bytes) else {
            return FrameResult::Invalid("invalid JSON or duplicate object key");
        };
        let Some(name) = record
            .get("meta")
            .and_then(|v| v.as_object())
            .and_then(|v| v.get("type"))
            .and_then(|v| v.as_str())
            .filter(|v| !v.is_empty())
        else {
            return FrameResult::Invalid("missing message type");
        };
        let Some(data) = record.get("data").and_then(|v| v.as_object()) else {
            return FrameResult::Invalid("missing data object");
        };
        if name != "GPS" {
            return FrameResult::Unsupported("only decoded GPS records are supported");
        }
        if !data.contains_key("TimeUS") {
            return FrameResult::Unsupported("explicit producer TimeUS required");
        }
        // Reuse the adopted semantic reader, including strict counters, field retention,
        // channel, fingerprint and BootRelative/unknown-wall handling. No second scaling.
        match MavlogJsonReader.read_report(&self.profile, bytes) {
            Ok(mut report) if report.observations.len() == 1 => {
                FrameResult::Observation(report.observations.remove(0))
            }
            _ => FrameResult::Invalid("invalid decoded GPS record or producer counter"),
        }
    }

    fn emit_frame(
        &mut self,
        terminated_by_lf: bool,
        emit: &mut impl FnMut(StreamEvent),
    ) -> Result<(), ReadError> {
        let result = if !terminated_by_lf {
            FrameResult::IncompleteEof
        } else if self.frame_bytes > MAX_FRAME_BYTES as u64 {
            FrameResult::Oversized
        } else {
            self.decode_frame()
        };
        let mut counts = self.counts;
        counts.frames = add(counts.frames, 1)?;
        let counter = match &result {
            FrameResult::Observation(_) => &mut counts.observations,
            FrameResult::Unsupported(_) => &mut counts.unsupported_frames,
            FrameResult::Invalid(_) => &mut counts.invalid_frames,
            FrameResult::Oversized => &mut counts.oversized_frames,
            FrameResult::IncompleteEof => &mut counts.incomplete_frames,
        };
        *counter = add(*counter, 1)?;
        self.counts = counts;
        let event = StreamEvent {
            source_id: self.source_id.clone(),
            arrival_unix_ms: self.arrival_unix_ms,
            frame_index: counts.frames,
            source_bytes: std::mem::take(&mut self.buffer),
            source_bytes_truncated: self.frame_bytes > MAX_FRAME_BYTES as u64,
            frame_bytes: self.frame_bytes,
            terminated_by_lf,
            platform_domain: PlatformDomain::Unknown,
            result,
            counts,
        };
        self.frame_bytes = 0;
        self.arrival_unix_ms = None;
        emit(event);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::profile::parse_profile;

    fn profile() -> FamilyProfile {
        parse_profile(
            include_str!("../../../profiles/declared/arduplane-jsonl/profile.toml"),
            "test",
        )
        .unwrap()
    }

    #[test]
    fn counter_overflow_is_terminal_without_wrapping() {
        let mut stream = GpsJsonlStream::new(profile(), "counter".into()).unwrap();
        stream.counts.bytes_received = u64::MAX;
        assert!(
            stream
                .push(b"x", None, |_| panic!("no fabricated event"))
                .is_err()
        );
        assert_eq!(stream.counts.bytes_received, u64::MAX);
        assert!(stream.push(b"", None, |_| {}).is_err());
        assert!(add(u64::MAX, 1).is_err());
    }

    #[test]
    fn wrong_units_and_invalid_source_ids_are_rejected() {
        for source in ["".to_string(), " padded".to_string(), "x".repeat(129)] {
            assert!(GpsJsonlStream::new(profile(), source).is_err());
        }
        let mut p = profile();
        p.field_units.insert("GPS.Alt".into(), "mm".into());
        assert!(GpsJsonlStream::new(p, "source".into()).is_err());
    }

    #[test]
    fn exact_limit_requires_a_delimiter_and_empty_lines_are_not_observations() {
        let mut row = br#"{"meta":{"type":"GPS"},"data":{"TimeUS":1,"pad":""#.to_vec();
        row.resize(MAX_FRAME_BYTES - 3, b'x');
        row.extend_from_slice(br#""}}"#);
        assert_eq!(row.len(), MAX_FRAME_BYTES);
        let mut events = Vec::new();
        let mut stream = GpsJsonlStream::new(profile(), "bounded".into()).unwrap();
        stream.push(&row, None, |e| events.push(e)).unwrap();
        assert!(events.is_empty());
        stream.push(b"\n", None, |e| events.push(e)).unwrap();
        assert!(matches!(events[0].result, FrameResult::Observation(_)));
        assert!(!events[0].source_bytes_truncated);
        stream.push(b"\n", None, |e| events.push(e)).unwrap();
        assert!(matches!(events[1].result, FrameResult::Invalid(_)));
        stream.finish(|e| events.push(e)).unwrap();
        assert_eq!(events.len(), 2);

        let mut tail = GpsJsonlStream::new(profile(), "unterminated".into()).unwrap();
        tail.push(&row, None, |_| panic!("no LF yet")).unwrap();
        tail.finish(|e| assert!(matches!(e.result, FrameResult::IncompleteEof)))
            .unwrap();
        assert_eq!(tail.counts().observations, 0);
    }
}
