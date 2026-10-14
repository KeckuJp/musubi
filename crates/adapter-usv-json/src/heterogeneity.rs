//! Heterogeneity walking-skeleton（異種 2 機種 → 1 Musubi → 1 C2 の North Star タイル）。
//!
//! 1 本の test で:
//! - UAV-MAVLink（`adapter-mavlink::MavlinkNormalizer` の hand-rolled MAVLink2 = **wrap**）と
//! - USV-JSON（本 crate `UsvNormalizer` の非 MAVLink テキスト JSON = **integrate**）
//! を **両方 normalize** し、**同一の `CotPublisher`** で **両方 CoT XML** にして、双方が
//! 有効な CoT（uid/lat/lon/point）になることを assert する。
//!
//! これが heterogeneous-input contract「一度つなぐだけで複数の C2 が安全に扱える」の機種非依存性の実装根拠
//! （2 機種が同一 COM PlatformState・同一 MARK/digest/CoT パイプラインに収束）。2nd C2 への
//! ファンアウトは S3（本 test は 1 C2 方言 = CoT で 2 機種収束を示すタイル）。
//!
//! 注: adapter-mavlink の `GOLDEN_FRAME` は `pub(crate)`（他 crate から不可視）なので、ここでは
//! MAVLINK SPEC golden_example の 40 バイトフレームを **独立に再構成**して UAV source に使う
//! （wire を実際に通すのが目的＝bytes を写経して MAVLink デコーダを通過させる）。

use super::UsvNormalizer;
use musubi_adapter_mavlink::MavlinkNormalizer;
use musubi_core::{Normalizer, RawObservation};
use musubi_northbound::{CotPublisher, NorthboundPublisher};
use musubi_types::ComObject;

/// UAV 側 source: MAVLink2 GLOBAL_POSITION_INT の 40 バイトフレーム（署名なし）。
/// lat=47.397742 / lon=8.545594 / alt=488 m。adapter-mavlink の S0 GOLDEN_FRAME と同一バイト列
/// （`pub(crate)` で借りられないため独立に持つ）。これは **MAVLink バイナリ wire**＝USV の
/// テキスト JSON とは別物（先頭 magic=0xFD のバイナリフレーム）。
const UAV_MAVLINK_FRAME: [u8; 40] = [
    0xFD, 0x1C, 0x00, 0x00, 0x00, 0x01, 0x01, 0x21, 0x00, 0x00, // header(10)
    0x39, 0x30, 0x00, 0x00, // time_boot_ms = 12345
    0x4C, 0x52, 0x40, 0x1C, // lat = 473977420
    0x44, 0xF4, 0x17, 0x05, // lon = 85455940
    0x40, 0x72, 0x07, 0x00, // alt = 488000 mm
    0x50, 0xC3, 0x00, 0x00, // relative_alt = 50000 mm
    0x64, 0x00, // vx = 100
    0x00, 0x00, // vy = 0
    0xF6, 0xFF, // vz = -10
    0x78, 0x69, // hdg = 27000 cdeg
    0x21, 0xA3, // CRC (CRC-16/MCRF4XX) — S0 は未検証
];

/// USV 側 source: 非 MAVLink テキスト JSON（vendor-ish `kecku-usv-telemetry/v1`）。
/// 先頭が `{` のテキスト＝MAVLink magic（0xFD/0xFE）が来ない＝真に異なる wire 形式。
const USV_JSON_FRAME: &str = r#"{
  "vehicle_id": "usv-jp-001",
  "lat": 35.4215,
  "lon": 139.6423,
  "heading_deg": 127.5,
  "alt_m": 1.4,
  "timestamp": "2024-09-15T03:42:17.123Z",
  "gnss_fix": 2,
  "mode": "AUTO"
}"#;

/// CoT XML から数値属性を抜く（round-trip 検証用・最小）。
fn attr_f64(xml: &str, key: &str) -> f64 {
    let pat = format!("{key}=\"");
    let start = xml.find(&pat).expect("attr present") + pat.len();
    let rest = &xml[start..];
    let end = rest.find('"').expect("attr close");
    rest[..end].parse::<f64>().expect("attr parses as f64")
}

#[test]
fn two_heterogeneous_sources_converge_to_one_c2_dialect() {
    // 異種2機種 → 1 Musubi → 1 C2（CoT）。両 source を **別経路で parse**（wrap / integrate）し、
    // **同一 CotPublisher** で CoT XML 化する。

    // --- UAV: MAVLink バイナリ wire を wrap 経路（標準デコーダ）で normalize ---
    let uav_env = MavlinkNormalizer::new()
        .normalize(&RawObservation {
            source_id: "uav-01".to_string(),
            payload: UAV_MAVLINK_FRAME.to_vec(),
            received_at: 0,
        })
        .expect("UAV MAVLink frame normalizes (wrap)");

    // --- USV: 非 MAVLink テキスト JSON を integrate 経路（自前 parse）で normalize ---
    let usv_env = UsvNormalizer::new()
        .normalize(&RawObservation {
            source_id: "usv-jp-001".to_string(),
            payload: USV_JSON_FRAME.as_bytes().to_vec(),
            received_at: 0,
        })
        .expect("USV JSON frame normalizes (integrate)");

    // --- COM 収束: どちらも同一の ComObject::PlatformState 型に落ちる ---
    let ComObject::PlatformState(uav_ps) = &uav_env.observation else {
        panic!("expected PlatformState")
    };
    let ComObject::PlatformState(usv_ps) = &usv_env.observation else {
        panic!("expected PlatformState")
    };
    // contract: MAVLink 側の platform_id は source_id と sysid の合成（共有チャネル demux）。
    assert_eq!(uav_ps.platform_id, "uav-01/sys1");
    // candidate A-2: USV 側の platform_id も source_id と vehicle_id の namespace 合成
    // （track aliasing 防止・この golden は両者が偶然同一文字列）。
    assert_eq!(usv_ps.platform_id, "usv-jp-001/usv-jp-001");

    // --- 北向き: 1 つの CotPublisher（書き直さない）で両方を CoT XML 化 ---
    let publisher = CotPublisher::new();
    assert_eq!(publisher.dialect(), "cot");
    let uav_xml =
        String::from_utf8(publisher.publish(&uav_env).expect("UAV publishes CoT")).expect("utf8");
    let usv_xml =
        String::from_utf8(publisher.publish(&usv_env).expect("USV publishes CoT")).expect("utf8");

    // --- 双方が有効な CoT（同一 uid/lat/lon/point 属性構造） ---
    for (label, xml, uid) in [
        ("uav", &uav_xml, "uav-01/sys1"), // contract: sysid demux 済み uid
        ("usv", &usv_xml, "usv-jp-001/usv-jp-001"), // candidate A-2: source_id/vehicle_id 合成 uid
    ] {
        assert!(
            xml.contains(&format!("uid=\"{uid}\"")),
            "{label} missing uid: {xml}"
        );
        assert!(xml.contains("<event "), "{label} missing <event>: {xml}");
        assert!(xml.contains("<point "), "{label} missing <point>: {xml}");
    }

    // --- field-level の意味が両 source で正しく CoT に出る（contract round-trip・許容誤差 1e-6） ---
    let uav_pos = uav_ps.position.as_ref().expect("uav position");
    assert!(
        (attr_f64(&uav_xml, "lat") - uav_pos.lat_deg).abs() <= 1e-6,
        "{uav_xml}"
    );
    assert!(
        (attr_f64(&uav_xml, "lon") - uav_pos.lon_deg).abs() <= 1e-6,
        "{uav_xml}"
    );
    assert!(uav_xml.contains("lat=\"47.397742\""), "{uav_xml}");

    let usv_pos = usv_ps.position.as_ref().expect("usv position");
    assert!(
        (attr_f64(&usv_xml, "lat") - usv_pos.lat_deg).abs() <= 1e-6,
        "{usv_xml}"
    );
    assert!(
        (attr_f64(&usv_xml, "lon") - usv_pos.lon_deg).abs() <= 1e-6,
        "{usv_xml}"
    );
    assert!(usv_xml.contains("lat=\"35.4215\""), "{usv_xml}");

    // --- 両 envelope が sealed（同一 contract base パイプライン）＝format 非依存の証拠 ---
    assert!(uav_env.content_digest.is_some(), "UAV must be sealed");
    assert!(usv_env.content_digest.is_some(), "USV must be sealed");

    // --- 異なる wire 形式から来た 2 機種が異なる位置を持つ（同一 source の取り違えでない） ---
    assert_ne!(
        uav_pos.lat_deg, usv_pos.lat_deg,
        "two distinct platforms must retain distinct positions"
    );
}

#[test]
fn uav_is_binary_usv_is_text_distinct_wire_formats() {
    // integrate を wrap に縮退させていない証拠（wire 形式が本当に別物）:
    // UAV フレームは MAVLink2 バイナリ（先頭 magic=0xFD）・USV は UTF-8 テキスト JSON（先頭 '{'）。
    assert_eq!(UAV_MAVLINK_FRAME[0], 0xFD, "UAV is MAVLink2 binary (STX)");
    assert_eq!(
        USV_JSON_FRAME.as_bytes()[0],
        b'{',
        "USV is text JSON, not a MAVLink magic byte"
    );
    // USV の wire には MAVLink magic（0xFD/0xFE）が先頭に来ない。
    let usv0 = USV_JSON_FRAME.as_bytes()[0];
    assert_ne!(usv0, 0xFD);
    assert_ne!(usv0, 0xFE);
}
