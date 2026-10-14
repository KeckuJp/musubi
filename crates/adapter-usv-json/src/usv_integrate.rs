//! S2 integrate の受入 oracle（非 MAVLink USV JSON → COM PlatformState）。
//!
//! regression property（USV JSON FORMAT spec §4 の eval 事項）を 1 つずつ落とす:
//! (1) gnss_fix=0 → INVALID（no-silent-drop）。
//! (2) heading 欠落 → DEGRADED(field-missing:heading)。
//! (3) alt 欠落 → Position.alt_m=None ＋ field-missing:alt 寄与。
//! (4) ISO 8601 UTC → time_confidence>=0.75 かつ observed_at=Some(i64)。
//! (5) timestamp 欠落 → time_confidence=0.2・observed_at=None・DEGRADED(time-suspect)。
//! (6) malformed JSON → INVALID(json-malformed)。
//! (7) OK golden → seal_digest→verify_digest が round-trip（digest-verified）。
//!
//! さらに integrate が **wrap に縮退していない**こと（COM 収束・CoT 再利用・read-only）も assert。

use super::{QueuedUsvTap, ReadOnlyUsvTap, UsvManifest, UsvNormalizer};
use musubi_core::{Normalizer, RawObservation, verify_digest};
use musubi_northbound::{CotPublisher, NorthboundPublisher};
use musubi_types::{ComObject, MarkStatus};

/// USV JSON FORMAT spec §2 の golden example（東京湾 横浜沖・全フィールド present）。
const GOLDEN_USV_JSON: &str = r#"{
  "vehicle_id": "usv-jp-001",
  "lat": 35.4215,
  "lon": 139.6423,
  "heading_deg": 127.5,
  "sog_knots": 6.2,
  "alt_m": 1.4,
  "timestamp": "2024-09-15T03:42:17.123Z",
  "gnss_fix": 2,
  "mode": "AUTO",
  "battery_pct": 78,
  "depth_m": 12.3,
  "source_app": "kecku-usv-nav-v1"
}"#;

fn raw(source_id: &str, json: &str) -> RawObservation {
    RawObservation {
        source_id: source_id.to_string(),
        payload: json.as_bytes().to_vec(),
        received_at: 0,
    }
}

// =====================================================================================
// COM 収束 ＋ field-level マッピング精度（contract・誤マップ=0・許容誤差 1e-6）
// =====================================================================================

#[test]
fn golden_usv_json_maps_to_platformstate_within_tolerance() {
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("golden USV JSON normalizes");

    // 異種 wire 形式（JSON）が UAV-MAVLink と **同一の COM PlatformState 型**に収束する。
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    let pos = ps.position.as_ref().expect("position present");

    // contract: lat/lon/alt の意味正規化（許容誤差 1e-6・field-level マッピング誤り=0）。
    assert!((pos.lat_deg - 35.4215).abs() < 1e-6, "lat={}", pos.lat_deg);
    assert!((pos.lon_deg - 139.6423).abs() < 1e-6, "lon={}", pos.lon_deg);
    assert!(
        (pos.alt_m.expect("alt present") - 1.4).abs() < 1e-6,
        "alt={:?}",
        pos.alt_m
    );

    // candidate A-2: platform_id は source_id と vehicle_id の namespace 合成（track aliasing 防止）。
    assert_eq!(ps.platform_id, "usv-jp-001/usv-jp-001");
    assert_eq!(ps.mode.as_deref(), Some("AUTO"));

    // ISO 8601 UTC timestamp present → observed_at が Some(i64)・time_confidence 高め。
    assert!(
        ps.timestamps.observed_at.is_some(),
        "observed_at must be set"
    );
    assert!(ps.timestamps.time_confidence >= 0.75);

    // sealed（content_digest 充填済み）・署名は S1 同様 None（contract・TOE 外）。
    assert!(env.content_digest.is_some(), "envelope must be sealed");
    assert!(env.signature.is_none());
}

#[test]
fn golden_full_fields_is_ok_with_provenance_preserved() {
    // 全フィールド present（alt/heading あり・ISO UTC・gnss_fix=2）→ 劣化事由ゼロ → OK。
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("normalizes");
    assert_eq!(env.claim.mark.status, MarkStatus::Ok);
    assert_eq!(env.claim.mark.reason_code, "normalized");

    // no-silent-drop: provenance に source / adapter / 中立来歴が順序保存で積まれる。
    let prov = &env.claim.mark.provenance;
    assert_eq!(prov[0], "source:usv-jp-001");
    assert_eq!(prov[1], "adapter:usv-telemetry-json/v1");
    assert!(prov.iter().any(|p| p == "gnss_fix:2"), "{prov:?}");
    assert!(prov.iter().any(|p| p == "alt-ref:MSL-assumed"), "{prov:?}");
    assert!(
        prov.iter().any(|p| p == "source-app:kecku-usv-nav-v1"),
        "{prov:?}"
    );
    // claim_id は namespace 合成済み platform_id ベース（MAVLink の命名規則に倣う・candidate A-2）。
    assert_eq!(
        env.claim.claim_id,
        "usv-jp-001/usv-jp-001:usv-telemetry-json"
    );
}

// =====================================================================================
// platform_id クロスアダプタ/クロスベンダー衝突（regression review）
// =====================================================================================

#[test]
fn same_self_declared_vehicle_id_from_two_taps_does_not_collide() {
    // 陽性テスト（contract A-2）: 同一アダプタ種別（USV JSON）× 複数インスタンス（艦隊/ベンダーが
    // 異なる 2 つの tap）が、デバイス自称の `vehicle_id` をたまたま同じ文字列で報告しても
    // （ベンダー既定値・番号付け衝突等でありうる）、Musubi 側の tap 識別子 `source_id`（このアダプタ
    // インスタンスが RawObservation に刻む一意な識別子）で namespace されるため、2 隻の異なる
    // 物理 USV が単一 platform_id に collapse（track aliasing）しないことを確認する。
    let json = r#"{"vehicle_id":"usv-01","lat":35.0,"lon":139.0,"timestamp":"2024-09-15T03:42:00Z","gnss_fix":2}"#;

    // fleet-a と fleet-b は異なる艦隊/ベンダーの tap（Musubi 側の source_id が異なる）だが、
    // 両方のデバイスが偶然同一の vehicle_id="usv-01" を自称している。
    let env_a = UsvNormalizer::new()
        .normalize(&raw("fleet-a-tap", json))
        .expect("fleet-a normalizes");
    let env_b = UsvNormalizer::new()
        .normalize(&raw("fleet-b-tap", json))
        .expect("fleet-b normalizes");

    let ComObject::PlatformState(ps_a) = &env_a.observation else {
        panic!("expected PlatformState")
    };
    let ComObject::PlatformState(ps_b) = &env_b.observation else {
        panic!("expected PlatformState")
    };

    assert_eq!(ps_a.platform_id, "fleet-a-tap/usv-01");
    assert_eq!(ps_b.platform_id, "fleet-b-tap/usv-01");
    assert_ne!(
        ps_a.platform_id, ps_b.platform_id,
        "two distinct physical USVs sharing a self-declared vehicle_id must not collapse to one platform_id"
    );
    // claim_id も同様に衝突しない（platform_id 起点なので自動的に分離される）。
    assert_ne!(env_a.claim.claim_id, env_b.claim.claim_id);
}

// =====================================================================================
// MARK 意味論（DEGRADED 寄与・regression property 2/3/5）
// =====================================================================================

#[test]
fn heading_absent_degrades_with_field_missing_heading() {
    // regression property (2): heading 欠落 → DEGRADED(field-missing:heading)。
    // spec §2 の欠落ケース（usv-jp-002・heading/alt 欠落・gnss_fix=1）。
    let json = r#"{
        "vehicle_id": "usv-jp-002",
        "lat": 35.4100,
        "lon": 139.6510,
        "sog_knots": 3.1,
        "timestamp": "2024-09-15T03:43:00Z",
        "gnss_fix": 1
    }"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-002", json))
        .expect("degraded frame still normalizes (not an error)");
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.contains("field-missing:heading"),
        "reason={}",
        env.claim.mark.reason_code
    );
}

#[test]
fn alt_absent_yields_none_and_field_missing_alt() {
    // regression property (3): alt 欠落 → Position.alt_m=None ＋ field-missing:alt が DEGRADED 寄与。
    let json = r#"{
        "vehicle_id": "usv-jp-002",
        "lat": 35.41,
        "lon": 139.651,
        "heading_deg": 90.0,
        "timestamp": "2024-09-15T03:43:00Z",
        "gnss_fix": 1
    }"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-002", json))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    let pos = ps.position.as_ref().expect("position present");
    assert_eq!(pos.alt_m, None, "missing alt_m must be None in COM");
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.contains("field-missing:alt"),
        "reason={}",
        env.claim.mark.reason_code
    );
}

#[test]
fn iso8601_utc_sets_observed_at_and_high_confidence() {
    // regression property (4): ISO 8601 UTC → time_confidence>=0.75 かつ observed_at=Some(i64)。
    // 2024-09-15T03:42:17.123Z の epoch を独立計算で検証（決定論）。
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert!(ps.timestamps.time_confidence >= 0.75);
    // 2024-09-15T03:42:17Z = 1726371737 s（既知 epoch）。.123 → +123000 us。
    let expected_us = 1_726_371_737_i64 * 1_000_000 + 123_000;
    assert_eq!(ps.timestamps.observed_at, Some(expected_us));
}

#[test]
fn epoch_seconds_timestamp_is_absolute_but_lower_confidence() {
    // spec §4-C: epoch 秒形式（数値）→ observed_at present・time_confidence=0.75。
    let json = r#"{"vehicle_id":"usv-jp-003","lat":35.0,"lon":139.0,"alt_m":0.0,"heading_deg":10.0,"timestamp":1726371737.5,"gnss_fix":2}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-003", json))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert_eq!(ps.timestamps.observed_at, Some(1_726_371_737_500_000));
    assert!((ps.timestamps.time_confidence - 0.75).abs() < 1e-6);
    // 0.75 > 0.5 閾値・他の劣化事由なし → OK。
    assert_eq!(env.claim.mark.status, MarkStatus::Ok);
}

#[test]
fn missing_timestamp_degrades_time_suspect() {
    // regression property (5): timestamp 欠落 → time_confidence=0.2・observed_at=None・DEGRADED(time-suspect)。
    let json = r#"{"vehicle_id":"usv-jp-004","lat":35.0,"lon":139.0,"alt_m":0.0,"heading_deg":10.0,"gnss_fix":2}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-004", json))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert_eq!(ps.timestamps.observed_at, None);
    assert!((ps.timestamps.time_confidence - 0.2).abs() < 1e-6);
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.starts_with("time-suspect"),
        "reason={}",
        env.claim.mark.reason_code
    );
}

#[test]
fn iso8601_without_timezone_falls_back_to_low_confidence() {
    // pitfall: timezone indicator（Z / ±HH:MM）が無い ISO 風文字列は絶対時刻として確定しない
    // → epoch-sec 相当以下（ここでは解析不能 0.2）に落とす。
    let json = r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"alt_m":0.0,"heading_deg":10.0,"timestamp":"2024-09-15T03:42:17","gnss_fix":2}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert_eq!(ps.timestamps.observed_at, None, "no tz = not absolute");
    assert!(ps.timestamps.time_confidence <= 0.75);
}

// =====================================================================================
// INVALID 経路（no-silent-drop・regression property 1/6・spec §4-E）
// =====================================================================================

#[test]
fn gnss_fix_zero_is_invalid_not_dropped() {
    // regression property (1): gnss_fix=0 → INVALID(gnss-fix-invalid)・位置を COM に載せない。
    // spec §2 の悪質入力ケース（usv-jp-003・gnss_fix=0）。
    let json = r#"{"vehicle_id":"usv-jp-003","lat":0.0,"lon":0.0,"timestamp":"2024-09-15T03:44:00Z","gnss_fix":0}"#;
    let err = UsvNormalizer::new()
        .normalize(&raw("usv-jp-003", json))
        .expect_err("gnss_fix=0 must not normalize");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "gnss-fix-invalid");
    // no-silent-drop: 具体的失敗事由を provenance に保持。
    assert!(
        err.mark.provenance.iter().any(|p| p == "gnss_fix:0"),
        "{:?}",
        err.mark.provenance
    );
    assert!(!err.mark.reason_code.is_empty());
}

#[test]
fn malformed_json_is_invalid_with_mark() {
    // regression property (6): malformed JSON → INVALID(json-malformed)（no-silent-drop・MARK 付き）。
    let err = UsvNormalizer::new()
        .normalize(&raw("usv-x", "{ this is not json "))
        .expect_err("malformed JSON must not normalize");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "json-malformed");
    assert!(!err.mark.reason_code.is_empty());
    // MAVLink adapter の decode 失敗経路と同じく source/adapter を provenance に保持。
    assert!(err.mark.provenance.iter().any(|p| p == "source:usv-x"));
}

#[test]
fn missing_required_field_is_invalid() {
    // 必須フィールド欠落（lat 欠落）→ INVALID(required-field-missing)。
    let json = r#"{"vehicle_id":"u","lon":139.0,"gnss_fix":2}"#;
    let err = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect_err("missing lat must not normalize");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "required-field-missing");
    assert!(err.mark.provenance.iter().any(|p| p == "missing-field:lat"));
}

#[test]
fn empty_vehicle_id_is_invalid() {
    // pitfall: vehicle_id が空文字列 → 必須欠落と同等扱い（INVALID）。
    let json = r#"{"vehicle_id":"  ","lat":35.0,"lon":139.0,"gnss_fix":2}"#;
    let err = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect_err("empty vehicle_id must not normalize");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "required-field-missing");
}

#[test]
fn null_field_treated_as_absent_not_error() {
    // spec: null は「欠落」と同義（存在するが null = フィールド欠落と同等）。
    // alt_m=null → None（INVALID にはしない・DEGRADED 寄与）。
    let json = r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"alt_m":null,"heading_deg":10.0,"timestamp":"2024-09-15T03:42:17.123Z","gnss_fix":2}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect("null alt is absent, not an error");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert_eq!(ps.position.as_ref().unwrap().alt_m, None);
    assert!(env.claim.mark.reason_code.contains("field-missing:alt"));
}

#[test]
fn unknown_fields_are_kept_in_provenance() {
    // pitfall: 未知フィールドは黙って無視せず provenance に "unknown-field:{key}" を残す。
    let json = r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"alt_m":0.0,"heading_deg":10.0,"timestamp":"2024-09-15T03:42:17.123Z","gnss_fix":2,"radar_contact":true,"sea_state":4}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect("unknown fields do not break parsing");
    let prov = &env.claim.mark.provenance;
    assert!(
        prov.iter().any(|p| p == "unknown-field:radar_contact"),
        "{prov:?}"
    );
    assert!(
        prov.iter().any(|p| p == "unknown-field:sea_state"),
        "{prov:?}"
    );
}

// =====================================================================================
// verify 指摘の回帰（独立 verifier が挙げた最小ケースを assert で固定する）
// =====================================================================================

#[test]
fn impossible_calendar_date_is_not_trusted_as_absolute_time() {
    // verify 指摘: hand-rolled ISO パーサが day を実月長で検証せず、2024-02-31 を高信頼
    // （tc=0.85）の絶対時刻として受理していた。不可能日付は絶対時刻と認めず低信頼へ降格する。
    let json = r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"alt_m":0.0,"heading_deg":10.0,"timestamp":"2024-02-31T00:00:00Z","gnss_fix":2}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    // 不可能日付 → 絶対時刻を主張しない（observed_at=None）・低信頼（<=0.5）。
    assert_eq!(
        ps.timestamps.observed_at, None,
        "impossible date must not become a trusted observed_at"
    );
    assert!(
        ps.timestamps.time_confidence <= 0.5,
        "tc={}",
        ps.timestamps.time_confidence
    );
    // 結果として time-suspect で DEGRADED（黙って高信頼に昇格しない）。
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.contains("time-suspect"),
        "reason={}",
        env.claim.mark.reason_code
    );
}

#[test]
fn valid_leap_day_is_accepted() {
    // 逆の保証（過剰に弾かない）: 2024 はうるう年なので 02-29 は有効な絶対時刻。
    let json = r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"alt_m":0.0,"heading_deg":10.0,"timestamp":"2024-02-29T00:00:00Z","gnss_fix":2}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert!(
        ps.timestamps.observed_at.is_some(),
        "valid leap day must be accepted as absolute time"
    );
    assert!(ps.timestamps.time_confidence >= 0.75);
}

#[test]
fn gnss_fix_absent_keeps_assumed_one_provenance() {
    // verify 指摘: gnss_fix 欠落は fix=1 相当として暗黙に扱い、中立注釈を必ず provenance に残す
    // （黙って昇格しない＝no-silent-drop）。INVALID にはしない。
    let json = r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"alt_m":0.0,"heading_deg":10.0,"timestamp":"2024-09-15T03:42:17.123Z"}"#;
    let env = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect("gnss_fix absent is not an error");
    assert_ne!(env.claim.mark.status, MarkStatus::Invalid);
    assert!(
        env.claim
            .mark
            .provenance
            .iter()
            .any(|p| p == "gnss_fix:absent-assumed-1"),
        "{:?}",
        env.claim.mark.provenance
    );
}

#[test]
fn lat_out_of_range_is_invalid_not_dropped() {
    // 値域外の lat（91）→ INVALID(position-out-of-range)・位置を COM に載せない（no-silent-drop）。
    let json = r#"{"vehicle_id":"u","lat":91.0,"lon":139.0,"gnss_fix":2}"#;
    let err = UsvNormalizer::new()
        .normalize(&raw("u", json))
        .expect_err("lat 91 is out of WGS-84 range");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "position-out-of-range");
}

// =====================================================================================
// contract: adapter-assigned confidence は上流由来を偽装しない（provenance 明示＋恒等性）
// =====================================================================================

#[test]
fn confidence_provenance_marks_adapter_assigned_origin() {
    // USV JSON wire に confidence フィールドは存在しない。`Claim.confidence` の 0.6 は
    // Musubi 自身が付す既定値であって観測由来の実測ではない＝それを黙って伝播値のように
    // 見せない（"confidence:adapter-assigned" を provenance に必ず残す・contract）。
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("normalizes");
    assert!(
        env.claim
            .mark
            .provenance
            .iter()
            .any(|p| p == "confidence:adapter-assigned"),
        "{:?}",
        env.claim.mark.provenance
    );
}

#[test]
fn confidence_is_a_constant_not_derived_from_the_wire() {
    // 恒等性 property（fabrication=0）: wire は confidence を一切持たないので、他の全フィールドを
    // 変えても Claim.confidence は変化してはならない。変化するなら「何もない所から confidence を
    // 捏造している」ことになる（唯一 honest な振る舞いは固定の既定値＋明示 provenance）。
    let full = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("normalizes");
    // 欠落フィールド多め（gnss_fix 欠落・alt/heading/timestamp 欠落）の DEGRADED 例と比較する。
    let degraded_json = r#"{"vehicle_id":"usv-jp-002","lat":35.0,"lon":139.0}"#;
    let degraded = UsvNormalizer::new()
        .normalize(&raw("usv-jp-002", degraded_json))
        .expect("minimal fields still normalize (DEGRADED)");
    assert_eq!(full.claim.confidence, degraded.claim.confidence);
    assert!((full.claim.confidence - 0.6).abs() < 1e-6);
}

// =====================================================================================
// regression property (7): seal → verify round-trip（contract base 再利用）
// =====================================================================================

#[test]
fn ok_golden_seals_and_verifies_roundtrip() {
    // regression property (7): OK の golden を seal_digest → verify_digest すると digest-verified で round-trip。
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("normalizes");
    // normalize 内で seal 済み。verify_digest（core 再利用）で OK。
    let mark = verify_digest(&env);
    assert_eq!(mark.status, MarkStatus::Ok);
    assert_eq!(mark.reason_code, "digest-verified");
}

#[test]
fn one_bit_meta_tamper_is_detected_for_usv() {
    // contract base が USV 経路でも効く（format 非依存）: seal 後 1-bit 改竄 → INVALID。
    let mut env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("normalizes");
    assert_eq!(verify_digest(&env).status, MarkStatus::Ok);
    let ComObject::PlatformState(ps) = &mut env.observation else {
        panic!("expected PlatformState")
    };
    let pos = ps.position.as_mut().expect("position present");
    pos.lat_deg = f64::from_bits(pos.lat_deg.to_bits() ^ 1);
    let mark = verify_digest(&env);
    assert_eq!(mark.status, MarkStatus::Invalid);
    assert_eq!(mark.reason_code, "meta-tamper-detected");
}

// =====================================================================================
// 北向き再利用: 同一 CotPublisher が USV envelope を受け付ける（format 非依存の実証）
// =====================================================================================

#[test]
fn usv_envelope_publishes_via_same_cot_publisher() {
    // S0 と **同一の CotPublisher**（書き直さない）が USV envelope から有効な CoT XML を出す。
    let env = UsvNormalizer::new()
        .normalize(&raw("usv-jp-001", GOLDEN_USV_JSON))
        .expect("normalizes");
    let publisher = CotPublisher::new();
    let xml = String::from_utf8(publisher.publish(&env).expect("publishes CoT")).expect("utf8");

    // UAV と同一の uid/lat/lon/point 属性構造（heterogeneous-input contract の機種非依存性）。
    // candidate A-2: uid は namespace 合成済み platform_id。
    assert!(xml.contains("uid=\"usv-jp-001/usv-jp-001\""), "{xml}");
    assert!(xml.contains("<event "), "{xml}");
    assert!(xml.contains("<point "), "{xml}");
    assert!(xml.contains("lat=\"35.4215\""), "{xml}");
    assert!(xml.contains("lon=\"139.6423\""), "{xml}");
    assert!(xml.contains("hae=\"1.4\""), "{xml}");
}

#[test]
fn no_silent_drop_for_all_usv_inputs() {
    // 全入力（OK / DEGRADED / 各 INVALID）で必ず MARK が付き reason_code が非空（黙って捨てない）。
    let inputs: Vec<&str> = vec![
        GOLDEN_USV_JSON,                                             // OK
        r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"gnss_fix":1}"#, // DEGRADED（alt/heading/time 欠落）
        r#"{"vehicle_id":"u","lat":0.0,"lon":0.0,"gnss_fix":0}"#,    // INVALID gnss-fix
        r#"{"lat":35.0,"lon":139.0,"gnss_fix":2}"#, // INVALID required-field-missing
        "not json at all",                          // INVALID json-malformed
        "{}",                                       // INVALID required-field-missing
    ];
    for json in inputs {
        let result = UsvNormalizer::new().normalize(&raw("u", json));
        let reason_nonempty = result.as_ref().map_or_else(
            |e| !e.mark.reason_code.is_empty(),
            |ok| !ok.claim.mark.reason_code.is_empty(),
        );
        assert!(
            reason_nonempty,
            "every outcome must carry MARK reason_code: {json}"
        );
    }
}

#[test]
fn usv_normalizer_never_fires_reserved_mark_values() {
    // S2 境界: USV 経路も MARK は OK/DEGRADED/INVALID のみ（予約 4 値を出さない＝
    // ORDER_UNKNOWN/WITHHELD/STALE_REVOCATION/SELF_ASSERTED_TIME〔contract §3.3〕）。
    let inputs = [
        GOLDEN_USV_JSON,
        r#"{"vehicle_id":"u","lat":35.0,"lon":139.0,"gnss_fix":1}"#,
        r#"{"vehicle_id":"u","lat":0.0,"lon":0.0,"gnss_fix":0}"#,
        "garbage",
    ];
    for json in inputs {
        let status = match UsvNormalizer::new().normalize(&raw("u", json)) {
            Ok(env) => env.claim.mark.status,
            Err(e) => e.mark.status,
        };
        assert!(
            matches!(
                status,
                MarkStatus::Ok | MarkStatus::Degraded | MarkStatus::Invalid
            ),
            "S2 must not emit reserved MarkStatus, got {status:?}"
        );
    }
}

// =====================================================================================
// §5 read-only 不変条件（FEAT-A1-a: egress_bytes=0 を Manifest 宣言 + test assert で固定）
// =====================================================================================

#[test]
fn read_only_manifest_declares_zero_egress() {
    // USV source も MAVLink tap と同じく egress_bytes=0・heartbeat 無し・command 無し。
    let m = UsvManifest::read_only();
    assert_eq!(m.egress_bytes, 0, "read-only tap must not egress");
    assert!(!m.emits_heartbeat, "must not emit heartbeat");
    assert!(
        !m.emits_command,
        "must not emit command (X-NOWRITE/contract)"
    );
}

#[test]
fn tap_poll_does_not_egress_regardless_of_input() {
    // poll は受信キューを消費する read 操作のみ＝どんな入力でも egress_bytes は 0 のまま。
    let frames = vec![
        GOLDEN_USV_JSON.as_bytes().to_vec(),
        b"garbage".to_vec(),
        r#"{"vehicle_id":"u","lat":0.0,"lon":0.0,"gnss_fix":0}"#
            .as_bytes()
            .to_vec(),
    ];
    let mut tap = QueuedUsvTap::new("usv-jp-001", frames);
    assert_eq!(tap.source_id(), "usv-jp-001");

    let mut polled = 0;
    while let Some(obs) = tap.poll() {
        polled += 1;
        // poll 後も egress 宣言は 0 のまま（送信が一切混入しない）。
        assert_eq!(tap.manifest().egress_bytes, 0);
        // normalize しても（成功/失敗に関わらず）adapter は送信口を持たない。
        let _ = UsvNormalizer::new().normalize(&obs);
        assert_eq!(tap.manifest().egress_bytes, 0);
    }
    assert_eq!(polled, 3, "all queued frames consumed read-only");
}
