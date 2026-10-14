//! contract: quarantine harness の接地点（ガードレール#2 実装接地＝the quarantine contract）。
//!
//! 既存の integrate 正規化ロジック（S2・usv_integrate.rs）は変えず、その手前にある
//! **追加**の quarantine 検査（payload size / JSON nesting / 文字列フィールドの危険符号点）
//! だけを検証する。

use super::UsvNormalizer;
use musubi_core::quarantine::QUARANTINE_REASON_CODE;
use musubi_core::{Normalizer, RawObservation};
use musubi_types::{ComObject, MarkStatus};

fn raw(source_id: &str, payload: Vec<u8>) -> RawObservation {
    RawObservation {
        source_id: source_id.to_string(),
        payload,
        received_at: 0,
    }
}

const GOLDEN: &str = r#"{
  "vehicle_id": "usv-jp-001",
  "lat": 35.4215,
  "lon": 139.6423,
  "timestamp": "2024-09-15T03:42:17.123Z",
  "gnss_fix": 2,
  "mode": "AUTO"
}"#;

#[test]
fn golden_json_still_normalizes_after_quarantine_wiring() {
    // 回帰: quarantine 追加後も既存 golden（一部任意フィールド欠落＝DEGRADED 許容）は無傷で通る。
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN.as_bytes().to_vec()))
        .expect("golden still normalizes");
    assert_ne!(env.claim.mark.status, MarkStatus::Invalid);
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    // candidate A-2: platform_id は source_id と vehicle_id の namespace 合成。
    assert_eq!(ps.platform_id, "usv-jp-001/usv-jp-001");
}

#[test]
fn oversize_payload_is_quarantined_before_json_parsing() {
    // 16 KiB を大幅に超える payload はプロトコル以前に reject（`serde_json` を試みない）。
    let huge = vec![b' '; 20 * 1024];
    let err = UsvNormalizer::new()
        .normalize(&raw("usv-huge", huge))
        .expect_err("oversize payload must be rejected");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:payload-oversize:")),
        "{:?}",
        err.mark.provenance
    );
}

#[test]
fn deeply_nested_unknown_field_is_quarantined_before_json_parsing() {
    // 敵対的に深いネストを持つ未知フィールド（`extra` の catch-all に流れ込みうる）は、
    // serde_json に渡す前の pre-parse 構造走査で reject する（stack-overflow 耐性）。
    let mut json = String::from(r#"{"vehicle_id":"u","lat":1.0,"lon":2.0,"deep":"#);
    json.push_str(&"[".repeat(64));
    json.push('1');
    json.push_str(&"]".repeat(64));
    json.push('}');
    let err = UsvNormalizer::new()
        .normalize(&raw("usv-deep", json.into_bytes()))
        .expect_err("deeply nested unknown field must be rejected");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:json-nesting-exceeded:")),
        "{:?}",
        err.mark.provenance
    );
}

#[test]
fn flat_json_with_many_neutral_notes_is_not_falsely_quarantined() {
    // 誤検知回避: 深くネストしていないが多くのキーを持つ JSON は quarantine を通過する
    // （nesting チェックはキー数でなく `{`/`[` の入れ子深さのみを見る）。
    let mut json = String::from(r#"{"vehicle_id":"u","lat":1.0,"lon":2.0"#);
    for i in 0..20 {
        json.push_str(&format!(r#","extra_{i}":{i}"#));
    }
    json.push('}');
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-wide", json.into_bytes()))
        .expect("wide-but-flat JSON must not be quarantined");
    assert_ne!(env.claim.mark.status, MarkStatus::Invalid);
}

#[test]
fn bidi_override_in_vehicle_id_is_quarantined() {
    // U+202E RIGHT-TO-LEFT OVERRIDE を含む vehicle_id は COM platform_id へ写る前に reject。
    let json = format!(
        r#"{{"vehicle_id":"usv{}-01","lat":1.0,"lon":2.0}}"#,
        '\u{202E}'
    );
    let err = UsvNormalizer::new()
        .normalize(&raw("usv-spoof", json.into_bytes()))
        .expect_err("bidi override in vehicle_id must be rejected");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:field-disallowed-codepoint:vehicle_id:")),
        "{:?}",
        err.mark.provenance
    );
}

#[test]
fn oversize_vehicle_id_is_quarantined_not_truncated() {
    let long_id = "u".repeat(300);
    let json = format!(r#"{{"vehicle_id":"{long_id}","lat":1.0,"lon":2.0}}"#);
    let err = UsvNormalizer::new()
        .normalize(&raw("usv-long", json.into_bytes()))
        .expect_err("oversize vehicle_id must be rejected, not silently truncated");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:field-oversize:vehicle_id:")),
        "{:?}",
        err.mark.provenance
    );
}

#[test]
fn zero_width_space_in_mode_is_quarantined() {
    // `mode` も非信頼識別子として検疫される（vehicle_id 限定でないことの回帰）。
    let json = format!(
        r#"{{"vehicle_id":"u","lat":1.0,"lon":2.0,"mode":"AUT{}O"}}"#,
        '\u{200B}'
    );
    let err = UsvNormalizer::new()
        .normalize(&raw("usv-mode-spoof", json.into_bytes()))
        .expect_err("zero-width space in mode must be rejected");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:field-disallowed-codepoint:mode:")),
        "{:?}",
        err.mark.provenance
    );
}
