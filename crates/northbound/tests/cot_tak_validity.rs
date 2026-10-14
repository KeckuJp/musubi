//! Integration: a CoT event Musubi emits is STRUCTURALLY valid for TAK ingest,
//! and the MARK token is human-readable in `<detail><remarks>`.
//!
//! Honest scope (grounding `cot_taky_atak`): this proves the emitted CoT is
//! **schema-valid per CoT 2.0** (the MITRE CoT Base-Event Schema shape — every
//! `use="required"` event/point attribute present + well-formed) and that the
//! MARK status token (OK / DEGRADED / WITHHELD / ORDER_UNKNOWN) is carried in
//! `<remarks>` so an operator can read it. It does **NOT** prove a real TAK
//! server accepted it, nor that an ATAK client rendered it on screen — those are
//! a manual harness (see `docs/offline-components.md`). Never claim "TAK
//! accepted"; the correct framing is "synthetic serializer output (self-attest)".
//!
//! Zero new deps: validation is a tiny hand-rolled attribute scanner over the
//! emitted XML string (NO external XML crate — keeps the publish path zero-dep,
//! per AGENTS.md §3). `musubi-types` is a runtime dep; this test only uses the
//! public `musubi_northbound::CotPublisher` + `musubi_types`.

use musubi_northbound::{CotPublisher, NorthboundPublisher};
use musubi_types::{
    Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
};
use std::fs;
use std::path::PathBuf;

// ----------------------------------------------------------------------------
// CoT 2.0 structural-validity predicate (hand-rolled, zero-dep)
// ----------------------------------------------------------------------------
//
// Grounding (MITRE CoT Base-Event Schema, use="required"):
//   <event> REQUIRES: version, type, uid, time, start, stale, how   (7)
//   <point> REQUIRES: lat, lon, hae, ce, le                          (5)
// Attribute ORDER is irrelevant to XSD validity — only PRESENCE + well-formed
// value matter. `<detail>` is optional and free-form; `<remarks>` is plain text.
//
// This predicate is deliberately strict so it has TEETH: a CoT missing any one
// required attribute (or with a malformed `time`) is REJECTED. The negative test
// below feeds it a deliberately-broken event and asserts rejection.

/// CoT 2.0 required `<event>` attributes (grounding: MITRE XSD use="required").
const REQUIRED_EVENT_ATTRS: [&str; 7] = ["version", "type", "uid", "time", "start", "stale", "how"];
/// CoT 2.0 required `<point>` attributes (grounding: MITRE XSD use="required").
const REQUIRED_POINT_ATTRS: [&str; 5] = ["lat", "lon", "hae", "ce", "le"];

/// Why a CoT sample failed the validity predicate (so test failures are legible).
#[derive(Debug, PartialEq, Eq)]
enum Invalid {
    NoEventTag,
    NoPointTag,
    MissingEventAttr(&'static str),
    MissingPointAttr(&'static str),
    BadDateTime(&'static str),
    NoRemarks,
    BadStaleOrdering,
}

/// Extract the raw attribute value for `key=".."` inside the substring `scope`
/// (the opening tag of an element). Returns the value between the quotes.
fn attr<'a>(scope: &'a str, key: &str) -> Option<&'a str> {
    let needle = format!("{key}=\"");
    let start = scope.find(&needle)? + needle.len();
    let rest = &scope[start..];
    let end = rest.find('"')?;
    Some(&rest[..end])
}

/// Slice the opening tag of element `name` (`<name ...>` — attrs live here).
fn open_tag<'a>(xml: &'a str, name: &str) -> Option<&'a str> {
    let open = format!("<{name}");
    let start = xml.find(&open)?;
    let rest = &xml[start..];
    let end = rest.find('>')?; // first '>' closes the opening tag (no '>' in our attr values)
    Some(&rest[..=end])
}

/// CoT dateTime sanity: `CCYY-MM-DDThh:mm:ss(.sss)?Z` (the shape the publisher
/// emits and a strict parser expects). Hand-rolled, no regex/chrono dep.
fn is_cot_datetime(s: &str) -> bool {
    let b = s.as_bytes();
    // Minimum "2026-06-23T08:30:00Z" = 20 chars; with millis "....000Z" = 24.
    if b.len() < 20 {
        return false;
    }
    let digit = |i: usize| b.get(i).is_some_and(u8::is_ascii_digit);
    let lit = |i: usize, c: u8| b.get(i) == Some(&c);
    digit(0) && digit(1) && digit(2) && digit(3)            // CCYY
        && lit(4, b'-') && digit(5) && digit(6)             // -MM
        && lit(7, b'-') && digit(8) && digit(9)             // -DD
        && lit(10, b'T')
        && digit(11) && digit(12) && lit(13, b':')          // hh:
        && digit(14) && digit(15) && lit(16, b':')          // mm:
        && digit(17) && digit(18)                           // ss
        && s.ends_with('Z')
}

/// The CoT 2.0 structural-validity predicate. `Ok(())` = schema-valid shape +
/// `<remarks>` present. `Err(Invalid)` names the first violation found.
///
/// This is the gate with teeth: it independently re-derives validity from the
/// emitted bytes rather than trusting the serializer.
fn validate_cot(xml: &str) -> Result<(), Invalid> {
    let event = open_tag(xml, "event").ok_or(Invalid::NoEventTag)?;
    for a in REQUIRED_EVENT_ATTRS {
        if attr(event, a).is_none() {
            return Err(Invalid::MissingEventAttr(a));
        }
    }

    let point = open_tag(xml, "point").ok_or(Invalid::NoPointTag)?;
    for a in REQUIRED_POINT_ATTRS {
        if attr(point, a).is_none() {
            return Err(Invalid::MissingPointAttr(a));
        }
    }

    // dateTime fields must be well-formed (a strict CoT parser rejects garbage).
    for a in ["time", "start", "stale"] {
        let v = attr(event, a).ok_or(Invalid::MissingEventAttr(a))?;
        if !is_cot_datetime(v) {
            return Err(Invalid::BadDateTime(a));
        }
    }

    // stale must be strictly after start (a degenerate/expired event is invalid
    // for live display). Lexicographic compare is valid for fixed-width ISO-8601 Z.
    let start = attr(event, "start").unwrap();
    let stale = attr(event, "stale").unwrap();
    if stale <= start {
        return Err(Invalid::BadStaleOrdering);
    }

    // Operator-readability channel: <remarks> must exist (it carries the MARK).
    if !xml.contains("<remarks>") || !xml.contains("</remarks>") {
        return Err(Invalid::NoRemarks);
    }

    Ok(())
}

/// Pull the text content of `<remarks>...</remarks>` (where the MARK token lives).
fn remarks_text(xml: &str) -> Option<&str> {
    let start = xml.find("<remarks>")? + "<remarks>".len();
    let rest = &xml[start..];
    let end = rest.find("</remarks>")?;
    Some(&rest[..end])
}

// ----------------------------------------------------------------------------
// Fixture builders (SYNTHETIC — fictional ids only, per AGENTS.md §6)
// ----------------------------------------------------------------------------

/// A fixed synthetic envelope at a fixed epoch so the emitted CoT is byte-stable
/// (golden-file pattern). `received_at = 0` → `1970-01-01T00:00:00.000Z`.
fn envelope_with_mark(status: MarkStatus, reason: &str) -> EvidenceEnvelope {
    EvidenceEnvelope {
        observation: ComObject::PlatformState(PlatformState {
            // synthetic, fictional id — NOT a real platform/customer name (§6).
            platform_id: "my-device-01".to_string(),
            position: Some(Position {
                lat_deg: 35.6586,
                lon_deg: 139.7454,
                alt_m: Some(12.0),
            }),
            mode: None,
            timestamps: Timestamps {
                observed_at: None,
                received_at: 0,
                time_confidence: 0.2,
            },
            // Domain-agnostic fixture (this test suite covers structural CoT validity /
            // MARK visibility, not domain symbology — see `air_domain_*` tests in
            // `crates/northbound/src/lib.rs` for the contract(b) domain-mapping coverage).
            platform_domain: musubi_types::PlatformDomain::Unknown,
        }),
        claim: Claim {
            claim_id: "c-1".to_string(),
            confidence: 0.6,
            mark: Mark {
                status,
                reason_code: reason.to_string(),
                provenance: vec!["source:my-device-01".to_string()],
            },
            // contractb-2: 本番アダプタは Some(AdapterAssigned) を出す＝golden も現実的な wire 値
            // （BASIS:adapter-assigned）を pin する。全 variant 網羅と calibration_id 非漏洩は
            // src/lib.rs の basis_* 単体テストが担う。
            confidence_basis: Some(musubi_types::ConfidenceBasis::AdapterAssigned),
        },
        classification: None,
        signature: None,
        content_digest: None,
        signer_id: None,
        signed_at: None,
        revocation_proof: None,
        trust_annotations: None,
    }
}

/// (status, reason, golden-fixture stem) — the MARK matrix this test pins.
/// Reasons are synthetic. ORDER_UNKNOWN/WITHHELD are reserved statuses the
/// adapter/fan-out path may attach; here we drive the publisher directly to
/// prove the CoT remarks channel renders each token an operator must read.
fn mark_matrix() -> [(MarkStatus, &'static str, &'static str); 4] {
    [
        (MarkStatus::Ok, "normalized", "cot_ok"),
        (MarkStatus::Degraded, "time-suspect", "cot_degraded"),
        (
            MarkStatus::Withheld,
            "sink-cannot-carry-mark",
            "cot_withheld",
        ),
        (
            MarkStatus::OrderUnknown,
            "vector-clock-incomparable",
            "cot_order_unknown",
        ),
    ]
}

fn fixtures_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
}

// ----------------------------------------------------------------------------
// Tests
// ----------------------------------------------------------------------------

/// Every MARK status: publish via the public CotPublisher, assert the emitted
/// CoT is structurally valid per CoT 2.0 AND the MARK token text is in <remarks>.
#[test]
fn each_mark_status_emits_structurally_valid_cot_with_mark_in_remarks() {
    for (status, reason, _stem) in mark_matrix() {
        let env = envelope_with_mark(status, reason);
        let bytes = CotPublisher::new().publish(&env).expect("publishes");
        let xml = String::from_utf8(bytes).expect("utf8");

        // (1) structurally valid per CoT 2.0 (all required event+point attrs,
        //     well-formed dateTimes, stale>start, remarks present).
        validate_cot(&xml)
            .unwrap_or_else(|e| panic!("status {status:?} invalid CoT: {e:?}\n{xml}"));

        // (2) the MARK token is human-readable in <remarks> (operator can read
        //     DEGRADED / WITHHELD / ORDER_UNKNOWN, not just OK).
        let token = match status {
            MarkStatus::Ok => "OK",
            MarkStatus::Degraded => "DEGRADED",
            MarkStatus::Withheld => "WITHHELD",
            MarkStatus::OrderUnknown => "ORDER_UNKNOWN",
            other => panic!("matrix should not contain {other:?}"),
        };
        let remarks = remarks_text(&xml).expect("remarks present");
        assert!(
            remarks.contains(token),
            "MARK token {token:?} not human-readable in <remarks> for {status:?}: {remarks:?}"
        );
        // reason_code rides along too (so the operator sees WHY).
        assert!(
            remarks.contains(reason),
            "reason_code {reason:?} missing from remarks for {status:?}: {remarks:?}"
        );
    }
}

/// Golden-file pattern: regenerate each status's CoT XML and assert byte-equality
/// with the committed fixture under tests/fixtures/cot_*.xml, so the wire format
/// can never silently drift. Set `UPDATE_COT_FIXTURES=1` to (re)write them.
#[test]
fn cot_golden_fixtures_do_not_drift() {
    let dir = fixtures_dir();
    let update = std::env::var_os("UPDATE_COT_FIXTURES").is_some();
    if update {
        fs::create_dir_all(&dir).expect("create fixtures dir");
    }

    for (status, reason, stem) in mark_matrix() {
        let env = envelope_with_mark(status, reason);
        let bytes = CotPublisher::new().publish(&env).expect("publishes");
        let produced = String::from_utf8(bytes).expect("utf8");
        let path = dir.join(format!("{stem}.xml"));

        if update {
            fs::write(&path, produced.as_bytes()).expect("write golden");
            continue;
        }

        let golden = fs::read_to_string(&path).unwrap_or_else(|e| {
            panic!(
                "missing golden fixture {}: {e}. \
                 Regenerate with `UPDATE_COT_FIXTURES=1 cargo test -p musubi-northbound \
                 --test cot_tak_validity`",
                path.display()
            )
        });
        assert_eq!(
            produced,
            golden,
            "CoT golden drift for {status:?} at {}: serializer output changed. \
             If intended, regenerate with UPDATE_COT_FIXTURES=1.",
            path.display()
        );
        // belt-and-suspenders: the committed golden is itself schema-valid.
        validate_cot(&golden).unwrap_or_else(|e| {
            panic!("committed golden {} is invalid CoT: {e:?}", path.display())
        });
    }
}

/// serializer policy: the committed goldens must also satisfy the **egress policy** (declared type +
/// declared detail tag + 4-field remarks grammar). This pins golden and policy to each
/// other, so neither can drift alone: regenerating a golden that the policy would refuse
/// (or tightening the policy past what the goldens carry) fails here.
#[test]
fn committed_goldens_satisfy_egress_policy() {
    for (status, _reason, stem) in mark_matrix() {
        let path = fixtures_dir().join(format!("{stem}.xml"));
        let golden = fs::read(&path)
            .unwrap_or_else(|e| panic!("missing golden fixture {}: {e}", path.display()));
        musubi_northbound::cot_policy::check_cot_event_bytes(&golden).unwrap_or_else(|v| {
            panic!(
                "committed golden {} ({status:?}) violates the CoT egress policy: {v}",
                path.display()
            )
        });
    }
}

/// TEETH (negative / mutation): the validity predicate must REJECT a CoT that is
/// missing required attributes — otherwise the positive tests prove nothing.
/// We mutate a known-good event in several ways and assert each is rejected,
/// then re-assert the unmutated baseline passes.
#[test]
fn validity_predicate_rejects_broken_cot() {
    // A deliberately-broken sample built inline: <event> with NO required attrs
    // and an empty <point> — a strict CoT parser rejects this.
    let broken_missing_all = "<event><point/></event>";
    assert!(
        matches!(
            validate_cot(broken_missing_all),
            Err(Invalid::MissingEventAttr(_))
        ),
        "predicate must reject an event with no required attrs"
    );

    // Baseline good event (mirrors the publisher's shape) — must PASS, so we know
    // the rejections below are due to the specific mutation, not an always-fail.
    let good = "<event version=\"2.0\" uid=\"my-device-01\" type=\"a-f-G\" how=\"m-g\" \
         time=\"1970-01-01T00:00:00.000Z\" start=\"1970-01-01T00:00:00.000Z\" \
         stale=\"1970-01-01T00:00:15.000Z\">\
         <point lat=\"35.6586\" lon=\"139.7454\" hae=\"12.0\" ce=\"9999999.0\" le=\"9999999.0\"/>\
         <detail><remarks>MARK:OK:normalized</remarks></detail></event>";
    assert_eq!(
        validate_cot(good),
        Ok(()),
        "baseline good event must validate"
    );

    // Mutation 1: drop a required event attr (`how` — the easy real-world bug).
    let no_how = good.replace(" how=\"m-g\"", "");
    assert_eq!(
        validate_cot(&no_how),
        Err(Invalid::MissingEventAttr("how")),
        "dropping required event attr `how` must be rejected"
    );

    // Mutation 2: drop a required point attr (`hae`).
    let no_hae = good.replace(" hae=\"12.0\"", "");
    assert_eq!(
        validate_cot(&no_hae),
        Err(Invalid::MissingPointAttr("hae")),
        "dropping required point attr `hae` must be rejected"
    );

    // Mutation 3: drop `ce`/`le` (CoT requires the unknown-error sentinel, not omission).
    let no_ce = good.replace(" ce=\"9999999.0\"", "");
    assert_eq!(
        validate_cot(&no_ce),
        Err(Invalid::MissingPointAttr("ce")),
        "omitting required point attr `ce` (instead of the 9999999.0 sentinel) must be rejected"
    );

    // Mutation 4: malformed dateTime in `time`.
    let bad_time = good.replace("time=\"1970-01-01T00:00:00.000Z\"", "time=\"not-a-date\"");
    assert_eq!(
        validate_cot(&bad_time),
        Err(Invalid::BadDateTime("time")),
        "malformed `time` dateTime must be rejected"
    );

    // Mutation 5: stale not after start (degenerate window).
    let bad_stale = good.replace(
        "stale=\"1970-01-01T00:00:15.000Z\"",
        "stale=\"1970-01-01T00:00:00.000Z\"",
    );
    assert_eq!(
        validate_cot(&bad_stale),
        Err(Invalid::BadStaleOrdering),
        "stale <= start must be rejected"
    );

    // Mutation 6: strip <remarks> (the operator-readability channel).
    let no_remarks = good.replace(
        "<detail><remarks>MARK:OK:normalized</remarks></detail>",
        "<detail/>",
    );
    assert_eq!(
        validate_cot(&no_remarks),
        Err(Invalid::NoRemarks),
        "missing <remarks> must be rejected (MARK would be unreadable)"
    );
}
