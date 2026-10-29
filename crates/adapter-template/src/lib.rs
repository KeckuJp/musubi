//! # musubi-adapter-template — author-your-own-adapter TEMPLATE (read-only)
//!
//! **What this crate is.** The copyable *shortest path* for a field implementer to wire
//! their OWN device into Musubi **read-only** and get evidence-tagged CoT out, without
//! touching `core` or `northbound`. Copy this crate, rename it, and rewrite **one
//! function** — [`parse_my_device`] — for your device's wire format. Everything else
//! (MARK derivation, sealing, CoT publish, the §5 read-only self-declaration, the tests
//! that are your correctness oracle) you keep almost verbatim.
//!
//! It mirrors the fuller worked **integrate** example `crates/adapter-usv-json`
//! (a vendor JSON wire). This template is intentionally smaller and **dependency-free**:
//! a fictional synthetic device `my-device` with a trivial whitespace-delimited text wire
//! that is parsed BY HAND (`str::split_whitespace` + `f64::parse`), **not** serde. That
//! keeps `Cargo.lock` free of new external deps and `core` no-net.
//!
//! ## The Normalizer contract (the ONE rule you must follow)
//!
//! Implement [`musubi_core::Normalizer`] (`fn normalize(&self, raw) -> Result<EvidenceEnvelope,
//! NormalizeError>`). On success return a **sealed** [`EvidenceEnvelope`] carrying a COM
//! [`PlatformState`]; on failure return a [`NormalizeError`] whose `Mark` is **INVALID**
//! (no-silent-drop — never return a silent `None`/drop).
//!
//! - Build quality **facts** as [`QualityNote`]s and let [`derive_mark`] decide OK vs
//!   DEGRADED. **Never hand-construct `MarkStatus::Ok` / `MarkStatus::Degraded`** in your
//!   adapter — the OK/DEGRADED vocabulary is owned by `core` so it can't drift per-adapter.
//! - Raise **INVALID** only via [`NormalizeError`] (use the [`invalid`] helper).
//! - **Never** fire the reserved MARK values `ORDER_UNKNOWN` / `WITHHELD` /
//!   `STALE_REVOCATION` / `SELF_ASSERTED_TIME` / `BUFFER_SATURATED` — those belong to the DDIL
//!   path / northbound fan-out / revocation / signature-verification downgrade boundary
//!   (contract §3.3) / the store-and-forward bounded-buffer eviction path (contract) respectively,
//!   not to an adapter. (contract: keep this count in sync with `MarkStatus` in
//!   `crates/types/src/lib.rs` — the `never_fires_reserved_mark_values` test below is a positive
//!   allow-list, not a name-by-name denylist, so it stays correct even if this prose drifts.)
//!
//! ## The 4-step shortest path (raw bytes → COM → derive_mark → seal → publish CoT)
//!
//! 1. **parse** the wire into a typed observation — [`parse_my_device`] (THE function you
//!    rewrite). Malformed input is surfaced as `INVALID`, never dropped.
//! 2. **map** the parsed fields onto the COM [`PlatformState`] (the [`UsvNormalizer`]-style
//!    semantic mapping inside [`MyDeviceNormalizer::normalize`]). Each mapped field carries
//!    a [`QualityNote`]; a missing optional field contributes a DEGRADED note.
//! 3. **derive_mark** from the collected notes ([`derive_mark`]) — core decides OK/DEGRADED.
//! 4. **seal** the single canonical node ([`seal_digest`]) into an [`EvidenceEnvelope`] and
//!    **publish** it through the SAME [`musubi_northbound::CotPublisher`] — see
//!    [`publish_my_device_cot`]. Do NOT write your own CoT serializer.
//!
//! ## §5 read-only self-declaration (the Musubi "manifest")
//!
//! [`MyDeviceManifest`] mirrors `UsvManifest`: `egress_bytes = 0`, no heartbeat, no command.
//! This is the read-only contract expressed as a value AND asserted by a test — not promised
//! in prose. The tap ([`QueuedMyDeviceTap`]) only **consumes** a receive queue via `poll()`
//! and holds no send API. Musubi core originates/transports NO command (kinetic or non-kinetic).
//!
//! ## Scope
//!
//! This is a source preview in-process template. **NOT in scope here**: WASI component packaging,
//! a multi-language SDK, or a live network tap — those are external validation. For the fuller integrate
//! pattern (own parser as a crate-scoped dep, richer field semantics, heterogeneity/2x2
//! proofs) read `crates/adapter-usv-json`.

use musubi_core::{
    NormalizeError, Normalizer, ObservationQuality, QualityNote, RawObservation, derive_mark,
    seal_digest,
};
use musubi_northbound::{CotPublisher, NorthboundPublisher, PublishError};
use musubi_types::{
    Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
};

/// Adapter slug (goes into provenance as `adapter:<slug>` and the `claim_id` suffix).
///
/// When you copy this template, rename this to your device family (e.g. `acme-uav/v1`).
const ADAPTER_SLUG: &str = "my-device-text/v1";

/// contract quarantine: raw wire-line byte upper bound (DoS-resistance broad net — ground it here
/// BEFORE any parsing, the same way `adapter-mavlink`/`adapter-usv-json` do). A real device wire
/// line has no business being this large; this is generous headroom for a 5-column text line, not
/// a device-specific semantic check (those stay in [`parse_my_device`]). When you copy this
/// template for your own wire, keep this call as the FIRST thing `normalize` does — see
/// `musubi_core::quarantine` for the shared harness (Guardrail #2 anchor: the quarantine contract).
const MAX_WIRE_LINE_BYTES: usize = 1024;

/// `time_confidence` for an absolute epoch-seconds timestamp.
///
/// We mirror the USV adapter's scale: an absolute epoch is trustworthy-ish (`> 0.5`
/// threshold ⇒ NOT time-suspect), a missing timestamp is low (`<= 0.5` ⇒ time-suspect).
/// If your device emits a fully-qualified ISO-8601 UTC instant you can justify a higher
/// number (the USV adapter uses 0.85 for that); a bare epoch is one notch lower.
const TIME_CONF_EPOCH_SEC: f32 = 0.75;
/// `time_confidence` when the timestamp is absent / unparseable (cannot claim absolute time).
const TIME_CONF_ABSENT: f32 = 0.2;

/// 1 second = 1_000_000 microseconds (`observed_at` is epoch microseconds, `i64`).
const MICROS_PER_SEC: i64 = 1_000_000;

// =====================================================================================
// §5 read-only Manifest + tap  (egress_bytes = 0, no heartbeat, no command — asserted by test)
// =====================================================================================

/// Read-only self-declaration for a `my-device` source.
///
/// This is the §5 read-only contract held as a **compile-time-fixed invariant** (not a
/// runtime-mutable counter): the tap only receives, so it egresses 0 bytes, emits no
/// heartbeat, and emits no command. A test asserts these three facts — the policy lives in
/// a test, not in prose. Mirrors `musubi_adapter_usv_json::UsvManifest`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MyDeviceManifest {
    /// Bytes this adapter sends southbound. A read-only tap is identically 0 (no injection).
    pub egress_bytes: u64,
    /// Whether this adapter emits a heartbeat. Read-only ⇒ `false`.
    pub emits_heartbeat: bool,
    /// Whether this adapter emits a command/tasking. Read-only ⇒ `false` (X-NOWRITE / §5).
    pub emits_command: bool,
}

impl MyDeviceManifest {
    /// The read-only declaration (every egress path closed). Keep these three values as-is
    /// when you copy the template — they ARE the §5 self-declaration.
    #[must_use]
    pub const fn read_only() -> Self {
        Self {
            egress_bytes: 0,
            emits_heartbeat: false,
            emits_command: false,
        }
    }
}

impl Default for MyDeviceManifest {
    fn default() -> Self {
        Self::read_only()
    }
}

/// Contract for a passively-sniffed `my-device` source (same shape as the MAVLink/USV taps).
///
/// `poll` only **pulls** the next raw observation; it never injects or sends. A real device
/// behind a bidirectional link is still consumed subscribe-only on the Musubi side — this
/// trait has no send/command method by construction.
pub trait ReadOnlyMyDeviceTap {
    /// This tap's source identifier.
    fn source_id(&self) -> &str;

    /// The read-only invariant declaration (egress_bytes = 0, no heartbeat, no command).
    fn manifest(&self) -> MyDeviceManifest;

    /// Pull the next raw observation (one wire line as bytes). Never injects or sends.
    fn poll(&mut self) -> Option<RawObservation>;
}

/// Minimal in-memory read-only tap: hands out wire lines from a fixed queue (sim/test).
///
/// In a real deployment this is replaced by an MQTT subscribe / serial read / UDP recv — all
/// of which are **receive-only**. `poll` just pops the queue (a read); it egresses nothing for
/// any input. Note there is deliberately no `send`/`connect` method on this type.
#[derive(Debug, Clone, Default)]
pub struct QueuedMyDeviceTap {
    source_id: String,
    queue: std::collections::VecDeque<Vec<u8>>,
}

impl QueuedMyDeviceTap {
    /// Build a tap from a source id and a list of already-received wire frames.
    #[must_use]
    pub fn new(source_id: impl Into<String>, frames: Vec<Vec<u8>>) -> Self {
        Self {
            source_id: source_id.into(),
            queue: frames.into_iter().collect(),
        }
    }
}

impl ReadOnlyMyDeviceTap for QueuedMyDeviceTap {
    fn source_id(&self) -> &str {
        &self.source_id
    }

    fn manifest(&self) -> MyDeviceManifest {
        // Consuming the receive queue only ⇒ we never send (read-only invariant).
        MyDeviceManifest::read_only()
    }

    fn poll(&mut self) -> Option<RawObservation> {
        // Pop one frame: a read operation. No injection / send / ack.
        // received_at: this in-memory queue is a sim/test tap with no real ingest layer
        // (contract — a real deployment's MQTT subscribe / serial read / UDP recv would stamp
        // the caller supplies the actual receipt time at that ingest boundary).
        self.queue.pop_front().map(|payload| RawObservation {
            source_id: self.source_id.clone(),
            payload,
            received_at: 0,
        })
    }
}

// =====================================================================================
// THE WIRE  —  the fictional synthetic device `my-device`
// =====================================================================================

/// The documented `my-device` wire format (clearly synthetic — NOT a real product).
///
/// One observation per line, **whitespace/CSV-separated**, in fixed column order:
///
/// ```text
/// lat lon alt heading ts
/// ```
///
/// | column   | meaning                              | unit            | required |
/// |----------|--------------------------------------|-----------------|----------|
/// | `lat`    | latitude (WGS-84)                    | decimal degrees | yes      |
/// | `lon`    | longitude (WGS-84)                   | decimal degrees | yes      |
/// | `alt`    | altitude above MSL (`-` = missing)   | metres          | optional |
/// | `heading`| heading, true north (`-` = missing)  | degrees 0..360  | optional |
/// | `ts`     | observation time (`-` = missing)     | Unix epoch secs | optional |
///
/// Example (golden): `35.4215 139.6423 12.0 127.5 1726371737`
///
/// `-` is the explicit "absent" token for an optional column (so the line keeps a fixed
/// arity and a missing field never silently shifts the columns). Commas are accepted as
/// separators too (treated like whitespace), so `35.4215,139.6423,12.0,127.5,1726371737`
/// parses identically.
///
/// > **When you copy this template, this struct + [`parse_my_device`] are what you replace**
/// > with your device's real fields and decoder. Everything below
/// > ([`MyDeviceNormalizer::normalize`] onward) stays the same.
#[derive(Debug, Clone, PartialEq)]
pub struct RawObs {
    /// Latitude, WGS-84 decimal degrees. Required.
    pub lat: f64,
    /// Longitude, WGS-84 decimal degrees. Required.
    pub lon: f64,
    /// Altitude above MSL in metres. `None` when the `-` token is present.
    pub alt_m: Option<f64>,
    /// Heading, degrees true. `None` when the `-` token is present.
    pub heading_deg: Option<f64>,
    /// Observation time, Unix epoch seconds. `None` when the `-` token is present.
    pub ts_epoch_s: Option<f64>,
}

/// Parse error from the wire decoder (carried into an INVALID MARK — never dropped).
///
/// `code` is a short, stable reason token (lands in the MARK `reason_code`); `detail` is the
/// specific failure (lands in provenance) so a wrong wire is diagnosable, not silently lost.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseErr {
    /// Short, closed reason token (e.g. `"wire-malformed"`, `"field-unparseable"`).
    pub code: &'static str,
    /// Specific human-readable detail (e.g. `"expected>=2 fields, got 1"`).
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

/// The explicit "this optional column is absent" token.
const ABSENT_TOKEN: &str = "-";

/// **THE ONE FUNCTION AN ADOPTER REWRITES.**
///
/// Decode one `my-device` wire line (`lat lon alt heading ts`) into a typed [`RawObs`].
/// This is the entire device-specific surface: change the field set, the column order, the
/// units, and the parsing here — and the rest of the pipeline (mapping, MARK, seal, CoT)
/// keeps working unchanged.
///
/// Contract this function must honour (so the rest of the template stays correct):
/// - **No silent drop.** Any unparseable input returns `Err(ParseErr)` with a specific
///   `detail`; it is never swallowed into a default value.
/// - **`-` means absent**, not zero — an absent optional column maps to `None` (which later
///   becomes a DEGRADED contribution), never to `0.0`.
/// - **Deterministic & side-effect-free** (no clock/network/global state): same bytes in ⇒
///   same `RawObs` out. (This is what makes the 誤マップ=0 golden test meaningful.)
///
/// Hand-parsed on purpose (no serde) to keep the template dependency-free.
///
/// # Errors
/// Returns [`ParseErr`] if the line is not UTF-8, has fewer than the 2 required columns
/// (`lat`,`lon`), or any present field fails to parse as a finite number.
pub fn parse_my_device(wire: &str) -> Result<RawObs, ParseErr> {
    // Split on whitespace OR commas (CSV-friendly), dropping empty tokens.
    let fields: Vec<&str> = wire
        .split(|c: char| c.is_whitespace() || c == ',')
        .filter(|t| !t.is_empty())
        .collect();

    // lat and lon are the only required columns. Fewer ⇒ malformed (surface, don't drop).
    if fields.len() < 2 {
        return Err(ParseErr::new(
            "wire-malformed",
            format!("expected at least 2 fields (lat lon), got {}", fields.len()),
        ));
    }

    // Required numeric field: must be present AND parse to a finite f64.
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

    // Optional numeric field: the `-` token ⇒ None; anything else must parse to a finite f64
    // (a malformed optional value is an error, NOT a silent None — no-silent-drop).
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

// =====================================================================================
// The normalizer  —  raw fields → COM PlatformState → derive_mark → seal  (KEEP THIS)
// =====================================================================================

/// `my-device` text → COM [`PlatformState`] normalizer (implements [`musubi_core::Normalizer`]).
///
/// Deterministic and read-only. Parses the wire via [`parse_my_device`], maps fields onto the
/// COM, builds quality notes, and lets `core::derive_mark` decide OK/DEGRADED. MARK derivation
/// and sealing are **reused from `core`** (never re-implemented per adapter) — that reuse is
/// the whole point: a brand-new device converges onto the same canonical Evidence/CoT pipeline.
#[derive(Debug, Clone, Default)]
pub struct MyDeviceNormalizer;

impl MyDeviceNormalizer {
    #[must_use]
    pub fn new() -> Self {
        Self
    }
}

/// Build an INVALID [`NormalizeError`] (the only way an adapter raises INVALID — no-silent-drop).
///
/// Keep `reason_code` to a closed set of short tokens; put the specific cause in `detail` so it
/// survives in provenance. Mirrors `adapter-usv-json`'s `invalid()` helper verbatim in spirit.
fn invalid(source_id: &str, reason_code: &str, detail: impl Into<String>) -> NormalizeError {
    NormalizeError {
        source_id: source_id.to_string(),
        mark: Mark {
            status: MarkStatus::Invalid,
            reason_code: reason_code.to_string(),
            provenance: vec![
                format!("source:{source_id}"),
                format!("adapter:{ADAPTER_SLUG}"),
                detail.into(),
            ],
        },
    }
}

/// contract quarantine boundary (Guardrail #2 anchor: the quarantine contract): maps a
/// [`musubi_core::quarantine::QuarantineReject`] into a [`NormalizeError`] using the SAME
/// reason-code token every adapter in this repo uses
/// ([`musubi_core::quarantine::QUARANTINE_REASON_CODE`]) — so the quarantine harness is visibly
/// ONE shared mechanism, not a per-adapter reinvention. Keep this helper (or an equivalent call
/// straight through to `musubi_core::quarantine::*`) when you copy this template.
fn quarantine_error(
    source_id: &str,
    reject: musubi_core::quarantine::QuarantineReject,
) -> NormalizeError {
    NormalizeError {
        source_id: source_id.to_string(),
        mark: Mark {
            status: MarkStatus::Invalid,
            reason_code: musubi_core::quarantine::QUARANTINE_REASON_CODE.to_string(),
            provenance: vec![
                format!("source:{source_id}"),
                format!("adapter:{ADAPTER_SLUG}"),
                format!("quarantine:{reject}"),
            ],
        },
    }
}

impl Normalizer for MyDeviceNormalizer {
    fn normalize(&self, raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError> {
        let source_id = &raw.source_id;

        // (0) contract quarantine boundary (Guardrail #2 anchor: the quarantine contract) — bound the
        //     raw wire-line size BEFORE any parsing. This is the FIRST thing your copy of this
        //     template should do too: every untrusted, adapter-origin byte sequence passes through
        //     the shared `musubi_core::quarantine` harness before it can influence COM/Claim/
        //     provenance, regardless of whether the ultimate upstream producer is a sensor, a human
        //     report, or (future increment) an AI suggester. If your device's wire has its own
        //     string/identifier fields (this synthetic wire does not — see [`RawObs`]), also call
        //     `musubi_core::quarantine::quarantine_str_field` on each one before mapping it onto the
        //     COM (see `adapter-usv-json`'s `vehicle_id`/`mode` handling for the worked pattern).
        musubi_core::quarantine::quarantine_payload_size(&raw.payload, MAX_WIRE_LINE_BYTES)
            .map_err(|reject| quarantine_error(source_id, reject))?;

        // (1) PARSE — the device-specific step. Bytes must be UTF-8 text; malformed input is
        //     surfaced as INVALID (never silently dropped). This is the only call that changes
        //     when you adapt the template to your wire.
        let text = std::str::from_utf8(&raw.payload)
            .map_err(|e| invalid(source_id, "wire-malformed", format!("not-utf8:{e}")))?;
        let obs = parse_my_device(text)
            .map_err(|e| invalid(source_id, e.code, format!("parse:{}", e.detail)))?;

        // (2) VALIDATE the position range (WGS-84). Out-of-range ⇒ INVALID, position not
        //     placed on the COM (no-silent-drop of a bad fix).
        if !(-90.0..=90.0).contains(&obs.lat) {
            return Err(invalid(
                source_id,
                "position-out-of-range",
                format!("lat-out-of-range:{}", obs.lat),
            ));
        }
        if !(-180.0..=180.0).contains(&obs.lon) {
            return Err(invalid(
                source_id,
                "position-out-of-range",
                format!("lon-out-of-range:{}", obs.lon),
            ));
        }

        // (3) MAP fields → COM PlatformState, collecting quality NOTES as we go.
        //     Rule: a present field ⇒ neutral note (kept in provenance, OK); a missing optional
        //     field ⇒ degraded note (DEGRADED contribution). We never set the MarkStatus here —
        //     derive_mark does that from these notes.
        let mut notes: Vec<QualityNote> = Vec::new();

        // alt: present ⇒ neutral provenance with the reference frame; absent ⇒ DEGRADED.
        if obs.alt_m.is_some() {
            // We carry the altitude reference assumption explicitly (self-describing evidence).
            notes.push(QualityNote::neutral("alt-ref:MSL-assumed"));
        } else {
            notes.push(QualityNote::degraded(
                "alt field absent",
                "field-missing:alt",
            ));
        }

        // heading: PlatformState has no heading slot, so present ⇒ neutral provenance,
        // absent ⇒ DEGRADED (mirrors the USV/MAVLink `field-missing:heading` treatment).
        if let Some(hd) = obs.heading_deg {
            notes.push(QualityNote::neutral(format!("heading_deg:{hd}")));
        } else {
            notes.push(QualityNote::degraded(
                "heading field absent",
                "field-missing:heading",
            ));
        }

        // (4) TIMESTAMP interpretation → observed_at + time_confidence (+ a provenance note).
        //     Present finite epoch ⇒ absolute time at moderate confidence; absent ⇒ low
        //     confidence (which derive_mark turns into a `time-suspect` DEGRADED).
        let (observed_at, time_confidence, time_note) = interpret_timestamp(obs.ts_epoch_s);
        notes.push(time_note);

        // contract: `Claim.confidence` below is a fixed adapter-assigned constant (0.6) — the
        // upstream wire has no confidence field at all. Do NOT silently invent a confidence
        // value and pass it off as a wire-derived measurement; if your device also has no
        // upstream confidence, keep this note so the fabrication stays visible in provenance
        // rather than looking like a propagated, cross-vendor-comparable number.
        notes.push(QualityNote::neutral("confidence:adapter-assigned"));

        // (5) Build the COM PlatformState — the SAME type a MAVLink/USV adapter produces, so a
        //     new device converges onto one canonical object.
        let position = Position {
            lat_deg: obs.lat,
            lon_deg: obs.lon,
            alt_m: obs.alt_m,
        };
        let timestamps = Timestamps {
            observed_at,
            // contract: the ingest layer (transport/tap) stamps the real receipt time on
            // `RawObservation::received_at`; the adapter just forwards it here (it no longer
            // hardcodes 0 — that hardcoding is what collapsed the dual-timestamp semantics).
            received_at: raw.received_at,
            time_confidence,
        };
        let platform = PlatformState {
            // `platform_id` identifies the track. regression review
            // (platform_id cross-adapter/cross-vendor collision — track aliasing): this synthetic
            // `my-device` wire has NO device-declared identity field of its own (see [`RawObs`] —
            // just lat/lon/alt/heading/ts), so `source_id` (the tap-level identifier the caller
            // stamps on every `RawObservation`, unique per adapter instance/physical connection)
            // is the ONLY identity Musubi has here — there is nothing else to compose it with.
            // This differs from `adapter-mavlink` (`{source_id}/sys{sysid}`, contract) and
            // `adapter-usv-json` (`{source_id}/{vehicle_id}`), which both fold a device-declared
            // wire field into the namespace on top of `source_id`. It is the caller's
            // responsibility to assign a distinct `source_id` per physical device when using this
            // template as-is. **If you copy this template for a real device that DOES expose its
            // own vehicle/serial field on the wire, follow the `adapter-usv-json` pattern and
            // compose `platform_id = format!("{source_id}/{device_declared_id}")` instead of
            // reusing `source_id` bare** — otherwise two physical units sharing that field's value
            // (vendor default, renumbering, etc.) collapse into one COM track exactly as A-2 found
            // for `adapter-usv-json`/`adapter-mavlink` before their fix.
            platform_id: source_id.clone(),
            position: Some(position),
            mode: None,
            timestamps,
            // contract(b): this template is domain-agnostic (copy-and-adapt for any device
            // class); leave `Unknown` here and have the copy's author set the correct
            // `PlatformDomain` (Air/Surface/Ground) once they know their vehicle class —
            // northbound maps `Unknown` to its own explicit symbology rather than silently
            // defaulting to Ground/friendly (the exact bug this fixes).
            platform_domain: musubi_types::PlatformDomain::Unknown,
        };

        // (6) DERIVE MARK from the collected quality facts — core decides OK vs DEGRADED.
        //     (We never construct Ok/Degraded ourselves, and never the reserved 5 values.)
        let quality = ObservationQuality {
            source_id: source_id.clone(),
            adapter_slug: ADAPTER_SLUG.to_string(),
            time_confidence,
            notes,
        };
        let mark = derive_mark(&quality);

        let claim = Claim {
            claim_id: format!("{source_id}:my-device-text"),
            // An observation claim, not a fact — a control plane must not auto-adopt it.
            //
            // contract: this 0.6 is an adapter-assigned constant, NOT something read off the
            // wire (this device's spec has no confidence field). Musubi does not fabricate a
            // measured confidence value here — it is asserting its own default claim-level
            // confidence, and the "confidence:adapter-assigned" note above makes that origin
            // explicit in `mark.provenance` (do not compare this number across vendors — there
            // is no calibration contract behind it). If your real device DOES publish a
            // confidence/quality field on the wire, map it here instead of copying this
            // constant, and drop the adapter-assigned note.
            confidence: 0.6,
            mark,
            // contract: the origin of this adapter-assigned default, expressed as a type as well
            // (paired with the "confidence:adapter-assigned" provenance note). If your real device
            // publishes a measured confidence, set `Measured { calibration_id }` instead.
            confidence_basis: Some(musubi_types::ConfidenceBasis::AdapterAssigned),
        };

        // (7) SEAL the single canonical node (content_digest filled; signature stays None —
        //     keys/signing are TOE-外, impl-deferred). Reuse core::seal_digest verbatim.
        //     contract §3.5 reserved fields (signer_id/signed_at/revocation_proof) stay None in
        //     source preview (excluded from canonical_bytes → golden unchanged; contract impl-deferred).
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

/// Interpret the optional epoch-seconds timestamp into `(observed_at_us, time_confidence, note)`.
///
/// Present & finite ⇒ absolute time at [`TIME_CONF_EPOCH_SEC`]; absent/non-finite ⇒ no absolute
/// time and [`TIME_CONF_ABSENT`] (which `derive_mark` reads as `time-suspect`). Deterministic —
/// it never reads a real clock.
fn interpret_timestamp(ts_epoch_s: Option<f64>) -> (Option<i64>, f32, QualityNote) {
    match ts_epoch_s {
        Some(epoch) if epoch.is_finite() => {
            // seconds × 1e6 → microseconds. Truncation of sub-microsecond is intentional.
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

// =====================================================================================
// Northbound  —  own-device → CoT by REUSE (seal already done in normalize; publish here)
// =====================================================================================

/// Publish a normalized `my-device` envelope as CoT XML via the SAME [`CotPublisher`].
///
/// This is step 4 of the shortest path made explicit. You do **not** write a CoT serializer:
/// you hand your already-sealed [`EvidenceEnvelope`] to the shared publisher and get
/// schema-valid CoT (`<event><point/><detail><remarks>…MARK…</remarks></detail></event>`).
/// The MARK rides in `<remarks>` as a visible downgrade (no-silent-drop), so a DEGRADED state
/// is human-readable in a TAK client's Remarks field without any plugin.
///
/// Connect once → many C2: the same envelope also feeds `NvgPublisher`/fan-out unchanged.
///
/// honest scope: this emits a **schema-valid / synthetic serializer output (self-attest)** — it is NOT
/// proof that a real TAK/Delta server accepted it (that is impl-deferred, external validation).
///
/// # Errors
/// Returns [`PublishError`] if the envelope cannot be serialized (e.g. missing position).
pub fn publish_my_device_cot(envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
    CotPublisher::new().publish(envelope)
}

#[cfg(test)]
mod template_tests;

#[cfg(test)]
mod quarantine_tests;
