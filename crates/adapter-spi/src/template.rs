//! A worked adapter, end to end, over a synthetic wire.
//!
//! This is the SDK half: the shortest path from device bytes to a sealed
//! [`EvidenceEnvelope`], written out once so that an adapter author copies it and rewrites
//! **one function** — [`parse_my_device`] — for their own wire. Everything after that step is
//! kept almost verbatim, because it is the part that must not drift per adapter.
//!
//! The device is fictional. `my-device` has a whitespace-delimited text wire parsed by hand
//! with `str::split_whitespace` and `f64::parse`, deliberately not with a serialization
//! library, so that the shortest path carries no dependency of its own.
//!
//! ## The four steps
//!
//! 1. **quarantine** the untrusted bytes before parsing them
//!    ([`musubi_core::quarantine`]).
//! 2. **parse** the wire into a typed observation — [`parse_my_device`], the one function you
//!    rewrite. Malformed input becomes INVALID; it is never dropped.
//! 3. **map** the parsed fields onto the COM [`PlatformState`], collecting a
//!    [`QualityNote`] per field as you go, and let [`derive_mark`] decide OK versus DEGRADED.
//! 4. **seal** the single canonical node with [`seal_digest`].
//!
//! Step 4 ends at a sealed envelope. Serializing that envelope into a particular downstream
//! dialect is a separate concern with a separate crate, and an adapter never writes its own
//! serializer: two adapters that each write one produce two dialects that drift.
//!
//! ## The rules that keep an adapter honest
//!
//! - Build quality **facts** as [`QualityNote`]s and let [`derive_mark`] decide the status.
//!   **Never hand-construct `MarkStatus::Ok` or `MarkStatus::Degraded`** in an adapter: that
//!   vocabulary belongs to the core so that it cannot drift per adapter.
//! - Raise INVALID only through [`musubi_core::NormalizeError`], via
//!   [`crate::spi::invalid`].
//! - **Never** fire the reserved MARK values. They belong to the store-and-forward, fan-out
//!   and signature-verification paths, not to an adapter. The
//!   `never_fires_reserved_mark_values` test states this as a positive allow-list rather than
//!   a name-by-name denylist, so it stays correct even when this prose drifts.

use crate::spi::{invalid, quarantine_error};
use musubi_core::{
    NormalizeError, Normalizer, ObservationQuality, QualityNote, RawObservation, derive_mark,
    seal_digest,
};
use musubi_types::{
    Claim, ComObject, ConfidenceBasis, EvidenceEnvelope, PlatformDomain, PlatformState, Position,
    Timestamps,
};

/// Adapter slug. Goes into provenance as `adapter:<slug>` and into the `claim_id` suffix.
///
/// Rename this to your device family when you copy the template.
pub const ADAPTER_SLUG: &str = "my-device-text/v1";

/// Upper bound on one raw wire line, in bytes.
///
/// A quarantine bound, applied **before** any parsing: a broad net against a hostile or
/// broken producer, not a device-specific semantic check. Those stay in
/// [`parse_my_device`]. When you copy this template, keep the quarantine call as the first
/// thing `normalize` does.
pub const MAX_WIRE_LINE_BYTES: usize = 1024;

/// `time_confidence` for an absolute epoch-seconds timestamp.
///
/// An absolute epoch is trustworthy-ish and sits above the core's degradation threshold; a
/// missing timestamp sits below it and therefore reads as `time-suspect`. A device that emits
/// a fully qualified UTC instant can justify a higher number than a bare epoch.
const TIME_CONF_EPOCH_SEC: f32 = 0.75;
/// `time_confidence` when the timestamp is absent or unparseable, so no absolute time is
/// claimed.
const TIME_CONF_ABSENT: f32 = 0.2;

/// One second in microseconds. `observed_at` is epoch microseconds.
const MICROS_PER_SEC: i64 = 1_000_000;

/// The explicit "this optional column is absent" token.
const ABSENT_TOKEN: &str = "-";

/// The documented `my-device` wire, one observation per line, in fixed column order:
///
/// ```text
/// lat lon alt heading ts
/// ```
///
/// | column    | meaning                             | unit            | required |
/// |-----------|-------------------------------------|-----------------|----------|
/// | `lat`     | latitude (WGS-84)                   | decimal degrees | yes      |
/// | `lon`     | longitude (WGS-84)                  | decimal degrees | yes      |
/// | `alt`     | altitude above MSL (`-` = missing)  | metres          | optional |
/// | `heading` | heading, true north (`-` = missing) | degrees 0..360  | optional |
/// | `ts`      | observation time (`-` = missing)    | Unix epoch secs | optional |
///
/// Example: `35.4215 139.6423 12.0 127.5 1726371737`
///
/// `-` is the explicit absent token, so a line keeps a fixed arity and a missing field never
/// silently shifts the columns. Commas are accepted as separators as well.
///
/// This struct and [`parse_my_device`] are what you replace. Everything from
/// [`MyDeviceNormalizer::normalize`] onwards stays.
#[derive(Debug, Clone, PartialEq)]
pub struct RawObs {
    /// Latitude, WGS-84 decimal degrees. Required.
    pub lat: f64,
    /// Longitude, WGS-84 decimal degrees. Required.
    pub lon: f64,
    /// Altitude above MSL in metres. `None` when the absent token is present.
    pub alt_m: Option<f64>,
    /// Heading, degrees true. `None` when the absent token is present.
    pub heading_deg: Option<f64>,
    /// Observation time, Unix epoch seconds. `None` when the absent token is present.
    pub ts_epoch_s: Option<f64>,
}

/// A parse failure, carried into an INVALID MARK rather than dropped.
///
/// `code` is a short stable token that lands in the MARK reason code; `detail` is the
/// specific failure and lands in provenance, so that a wrong wire is diagnosable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseErr {
    /// Short, closed reason token, for example `"wire-malformed"` or `"field-unparseable"`.
    pub code: &'static str,
    /// The specific failure, for example `"expected at least 2 fields, got 1"`.
    pub detail: String,
}

impl ParseErr {
    fn new(code: &'static str, detail: impl Into<String>) -> Self {
        Self {
            code,
            detail: detail.into(),
        }
    }
}

/// **The one function an adopter rewrites.**
///
/// Decode one `my-device` wire line into a typed [`RawObs`]. This is the entire
/// device-specific surface: change the field set, the column order, the units and the parsing
/// here, and the rest of the pipeline keeps working.
///
/// The contract this function honours, which is what keeps the rest of the template correct:
///
/// - **No silent drop.** Unparseable input returns `Err(ParseErr)` with a specific detail; it
///   is never swallowed into a default value.
/// - **The absent token means absent, not zero.** A missing optional column maps to `None`,
///   which later becomes a DEGRADED contribution, and never to `0.0`.
/// - **Deterministic and side-effect free.** No clock, no network, no global state: the same
///   bytes in produce the same `RawObs` out. That is what makes a golden test meaningful.
///
/// # Errors
/// Returns [`ParseErr`] when the line has fewer than the two required columns, or when a
/// present field does not parse as a finite number.
pub fn parse_my_device(wire: &str) -> Result<RawObs, ParseErr> {
    // Split on whitespace or commas, dropping empty tokens.
    let fields: Vec<&str> = wire
        .split(|c: char| c.is_whitespace() || c == ',')
        .filter(|t| !t.is_empty())
        .collect();

    // lat and lon are the only required columns. Fewer means malformed: surface, do not drop.
    if fields.len() < 2 {
        return Err(ParseErr::new(
            "wire-malformed",
            format!("expected at least 2 fields (lat lon), got {}", fields.len()),
        ));
    }

    // A required numeric field must be present and parse to a finite f64.
    let parse_required = |idx: usize, name: &str| -> Result<f64, ParseErr> {
        let tok = fields[idx];
        let v: f64 = tok
            .parse()
            .map_err(|_| ParseErr::new("field-unparseable", format!("{name}={tok:?}")))?;
        if v.is_finite() {
            Ok(v)
        } else {
            Err(ParseErr::new(
                "field-unparseable",
                format!("{name}-not-finite:{tok:?}"),
            ))
        }
    };

    // An optional numeric field: the absent token yields None; anything else must parse to a
    // finite f64. A malformed optional value is an error, not a silent None.
    let parse_optional = |idx: usize, name: &str| -> Result<Option<f64>, ParseErr> {
        match fields.get(idx) {
            None | Some(&ABSENT_TOKEN) => Ok(None),
            Some(tok) => {
                let v: f64 = tok
                    .parse()
                    .map_err(|_| ParseErr::new("field-unparseable", format!("{name}={tok:?}")))?;
                if v.is_finite() {
                    Ok(Some(v))
                } else {
                    Err(ParseErr::new(
                        "field-unparseable",
                        format!("{name}-not-finite:{tok:?}"),
                    ))
                }
            }
        }
    };

    Ok(RawObs {
        lat: parse_required(0, "lat")?,
        lon: parse_required(1, "lon")?,
        alt_m: parse_optional(2, "alt")?,
        heading_deg: parse_optional(3, "heading")?,
        ts_epoch_s: parse_optional(4, "ts")?,
    })
}

/// The `my-device` normalizer: text wire to COM [`PlatformState`].
///
/// Deterministic and read-only. MARK derivation and sealing are reused from the core and are
/// never re-implemented per adapter. That reuse is the point: a brand-new device converges
/// onto the same canonical Evidence.
#[derive(Debug, Clone, Default)]
pub struct MyDeviceNormalizer;

impl MyDeviceNormalizer {
    /// Build the normalizer. It holds no state.
    #[must_use]
    pub fn new() -> Self {
        Self
    }
}

impl Normalizer for MyDeviceNormalizer {
    fn normalize(&self, raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError> {
        let source_id = &raw.source_id;

        // (1) Quarantine before parsing. Every untrusted, adapter-origin byte sequence passes
        //     the shared harness before it can influence the COM, the claim or provenance.
        //     If your wire carries string or identifier fields, also call
        //     `musubi_core::quarantine::quarantine_str_field` on each one before mapping it.
        musubi_core::quarantine::quarantine_payload_size(&raw.payload, MAX_WIRE_LINE_BYTES)
            .map_err(|reject| quarantine_error(source_id, ADAPTER_SLUG, reject))?;

        // (2) Parse: the device-specific step, and the only call that changes when you adapt
        //     this template to your own wire.
        let text = std::str::from_utf8(&raw.payload).map_err(|e| {
            invalid(
                source_id,
                ADAPTER_SLUG,
                "wire-malformed",
                format!("not-utf8:{e}"),
            )
        })?;
        let obs = parse_my_device(text).map_err(|e| {
            invalid(
                source_id,
                ADAPTER_SLUG,
                e.code,
                format!("parse:{}", e.detail),
            )
        })?;

        // (3) Validate the position range. Out of range is INVALID, and the position is not
        //     placed on the COM: a bad fix is refused, not quietly carried.
        if !(-90.0..=90.0).contains(&obs.lat) {
            return Err(invalid(
                source_id,
                ADAPTER_SLUG,
                "position-out-of-range",
                format!("lat-out-of-range:{}", obs.lat),
            ));
        }
        if !(-180.0..=180.0).contains(&obs.lon) {
            return Err(invalid(
                source_id,
                ADAPTER_SLUG,
                "position-out-of-range",
                format!("lon-out-of-range:{}", obs.lon),
            ));
        }

        // (4) Map fields onto the COM, collecting quality notes. A present field yields a
        //     neutral note kept in provenance; a missing optional field yields a degraded
        //     note. The status is never set here: derive_mark decides it from these notes.
        let mut notes: Vec<QualityNote> = Vec::new();

        if obs.alt_m.is_some() {
            // Carry the altitude reference assumption explicitly: self-describing evidence.
            notes.push(QualityNote::neutral("alt-ref:MSL-assumed"));
        } else {
            notes.push(QualityNote::degraded(
                "alt field absent",
                "field-missing:alt",
            ));
        }

        // PlatformState has no heading slot, so a present heading becomes neutral provenance
        // and an absent one contributes a degradation.
        if let Some(hd) = obs.heading_deg {
            notes.push(QualityNote::neutral(format!("heading_deg:{hd}")));
        } else {
            notes.push(QualityNote::degraded(
                "heading field absent",
                "field-missing:heading",
            ));
        }

        let (observed_at, time_confidence, time_note) = interpret_timestamp(obs.ts_epoch_s);
        notes.push(time_note);

        // The claim confidence below is a fixed adapter-assigned constant: this wire has no
        // confidence field at all. Do not invent a confidence value and present it as
        // wire-derived. Keeping this note makes the origin visible in provenance instead of
        // letting the number look like a propagated, cross-vendor-comparable measurement.
        notes.push(QualityNote::neutral("confidence:adapter-assigned"));

        let position = Position {
            lat_deg: obs.lat,
            lon_deg: obs.lon,
            alt_m: obs.alt_m,
        };
        let timestamps = Timestamps {
            observed_at,
            // The ingest layer stamps the real reception time on the raw observation; the
            // adapter forwards it. Hard-coding a zero here is what collapses the two-time
            // semantics into one.
            received_at: raw.received_at,
            time_confidence,
        };
        let platform = PlatformState {
            // `platform_id` identifies the track. This synthetic wire has no device-declared
            // identity field of its own, so the tap-level `source_id` is the only identity
            // available and there is nothing to compose it with. The caller is therefore
            // responsible for assigning a distinct `source_id` per physical device.
            //
            // **If your device does expose its own vehicle or serial field, compose
            // `platform_id = format!("{source_id}/{device_declared_id}")` instead of reusing
            // `source_id` bare.** Otherwise two physical units that share a default value for
            // that field collapse into one COM track.
            platform_id: source_id.clone(),
            position: Some(position),
            mode: None,
            timestamps,
            // This template is domain-agnostic, so it declares nothing. Set the correct
            // domain in your copy once you know your platform class. A reader maps `Unknown`
            // to its own distinct rendering rather than defaulting it to one of the three.
            platform_domain: PlatformDomain::Unknown,
        };

        // (5) Derive the MARK from the collected quality facts: the core decides OK versus
        //     DEGRADED, and the adapter never constructs either itself.
        let quality = ObservationQuality {
            source_id: source_id.clone(),
            adapter_slug: ADAPTER_SLUG.to_string(),
            time_confidence,
            notes,
        };
        let mark = derive_mark(&quality);

        let claim = Claim {
            claim_id: format!("{source_id}:my-device-text"),
            // An observation claim, not a fact. Nothing downstream may auto-adopt it.
            //
            // This 0.6 is adapter-assigned, not read off the wire, because this device's
            // wire has no confidence field. Do not compare this number across vendors: there
            // is no calibration contract behind it. If your device does publish a confidence,
            // map it here and drop the adapter-assigned note above.
            confidence: 0.6,
            mark,
            // The origin of that default, stated as a type as well as a note. If your device
            // publishes a measured confidence, set `Measured { calibration_id }` instead.
            confidence_basis: Some(ConfidenceBasis::AdapterAssigned),
        };

        // (6) Seal the single canonical node. `content_digest` is filled; `signature` stays
        //     `None`, because keys and signing are outside this preview.
        Ok(seal_digest(EvidenceEnvelope {
            observation: ComObject::PlatformState(platform),
            claim,
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }))
    }
}

/// Interpret the optional epoch-seconds timestamp into observed time, confidence and a note.
///
/// Present and finite yields an absolute time at [`TIME_CONF_EPOCH_SEC`]; absent or
/// non-finite yields no absolute time and [`TIME_CONF_ABSENT`], which the core reads as
/// `time-suspect`. Deterministic: it never reads a real clock.
fn interpret_timestamp(ts_epoch_s: Option<f64>) -> (Option<i64>, f32, QualityNote) {
    match ts_epoch_s {
        Some(epoch) if epoch.is_finite() => {
            // Seconds to microseconds. Truncation below a microsecond is intentional.
            #[allow(
                clippy::cast_possible_truncation,
                clippy::cast_precision_loss,
                clippy::cast_sign_loss
            )]
            let micros = (epoch * MICROS_PER_SEC as f64) as i64;
            (
                Some(micros),
                TIME_CONF_EPOCH_SEC,
                QualityNote::neutral("timestamp-format:epoch-sec"),
            )
        }
        _ => (
            None,
            TIME_CONF_ABSENT,
            QualityNote::neutral("timestamp:absent"),
        ),
    }
}
