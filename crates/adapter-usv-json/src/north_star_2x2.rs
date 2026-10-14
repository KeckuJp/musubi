//! North Star **2x2 タイル** walking-skeleton（異種 2 機種 × 2 C2 ＝ 4 出力）。
//!
//! the documented interface「2 機種 × 2 C2 タイル」の完成形:
//! - 機種軸（S2 完了）: UAV-MAVLink（バイナリ wire・**wrap**）＋ USV-JSON（テキスト wire・**integrate**）。
//! - C2 軸（S3 で追加）: CoT（TAK 方言）＋ NVG 2.0.2（NATO Vector Graphics）。
//!
//! 1 本の test で 2 機種を別経路で normalize → **各機種の単一 sealed Evidence 正本**を
//! `fanout::fan_out` で **2 C2 方言へ no-silent-drop fan-out** し、4 出力（UAV→CoT・UAV→NVG・
//! USV→CoT・USV→NVG）が全て有効になることを assert する。
//!
//! honest scope: CoT/NVG は **NATO 標準サブセット＝synthetic serializer output**（self-attest）であって
//! 外部consumer が受理した証明ではない（external validation・contract）。

use super::UsvNormalizer;
use musubi_adapter_mavlink::MavlinkNormalizer;
use musubi_core::{Normalizer, RawObservation};
use musubi_northbound::fanout::{SinkOutcome, fan_out};
use musubi_northbound::{CotPublisher, NorthboundPublisher, NvgPublisher};
use musubi_types::ComObject;

/// UAV 側 source: MAVLink2 GLOBAL_POSITION_INT の 40 バイトフレーム（heterogeneity.rs と同一）。
/// lat=47.397742 / lon=8.545594 / alt=488 m。
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
    0x21, 0xA3, // CRC — S0 未検証
];

/// USV 側 source: 非 MAVLink テキスト JSON（vendor-ish `kecku-usv-telemetry/v1`）。
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

#[test]
fn two_platforms_times_two_c2_dialects_all_four_outputs_valid() {
    // === 機種軸: 2 機種を別経路で normalize（wrap / integrate） ===
    let uav_env = MavlinkNormalizer::new()
        .normalize(&RawObservation {
            source_id: "uav-01".to_string(),
            payload: UAV_MAVLINK_FRAME.to_vec(),
            received_at: 0,
        })
        .expect("UAV MAVLink normalizes (wrap)");
    let usv_env = UsvNormalizer::new()
        .normalize(&RawObservation {
            source_id: "usv-jp-001".to_string(),
            payload: USV_JSON_FRAME.as_bytes().to_vec(),
            received_at: 0,
        })
        .expect("USV JSON normalizes (integrate)");

    // 両 envelope は normalize 内で seal 済み（単一正本）。
    assert!(uav_env.content_digest.is_some(), "UAV must be sealed");
    assert!(usv_env.content_digest.is_some(), "USV must be sealed");

    // === C2 軸: 各機種の単一正本を CoT + NVG の 2 方言へ no-silent-drop fan-out ===
    let cot = CotPublisher::new();
    let nvg = NvgPublisher::new();
    let publishers: [&dyn NorthboundPublisher; 2] = [&cot, &nvg];

    let uav_report = fan_out(&uav_env, &publishers);
    let usv_report = fan_out(&usv_env, &publishers);

    // 各機種で 2 C2 出力（4 出力合計・WITHHELD 無し＝両 envelope とも MARK を運べる sink へ）。
    assert_eq!(uav_report.published_count(), 2, "UAV → CoT + NVG");
    assert_eq!(usv_report.published_count(), 2, "USV → CoT + NVG");
    assert_eq!(uav_report.withheld_count(), 0);
    assert_eq!(usv_report.withheld_count(), 0);

    // 4 出力それぞれが有効な方言（CoT は <event>・NVG は <nvg>）で正しい platform_id を運ぶ。
    let checks = [
        // contract: MAVLink 側の uid は source_id と sysid の合成（共有チャネル demux）。
        (&uav_report, "uav-01/sys1", 47.397_742, 8.545_594),
        // candidate A-2: USV 側の uid も source_id と vehicle_id の合成（この golden は source_id
        // と vehicle_id が偶然同一文字列だが、namespace 合成なので "usv-jp-001/usv-jp-001" になる）。
        (&usv_report, "usv-jp-001/usv-jp-001", 35.4215, 139.6423),
    ];
    for (report, id, lat, lon) in checks {
        // CoT 出力。
        let cot_out = report.outcome_for("cot").expect("cot outcome");
        let cot_xml = String::from_utf8(cot_out.bytes().expect("cot bytes").to_vec()).unwrap();
        assert!(cot_xml.contains("<event "), "{id} CoT: {cot_xml}");
        assert!(cot_xml.contains(&format!("uid=\"{id}\"")), "{cot_xml}");
        assert!((attr_f64(&cot_xml, "lat") - lat).abs() <= 1e-6, "{cot_xml}");
        assert!((attr_f64(&cot_xml, "lon") - lon).abs() <= 1e-6, "{cot_xml}");

        // NVG 出力（x=lon・y=lat の逆順に注意）。
        let nvg_out = report.outcome_for("nvg").expect("nvg outcome");
        let nvg_xml = String::from_utf8(nvg_out.bytes().expect("nvg bytes").to_vec()).unwrap();
        assert!(nvg_xml.contains("<nvg "), "{id} NVG: {nvg_xml}");
        assert!(nvg_xml.contains(&format!("uri=\"{id}\"")), "{nvg_xml}");
        assert!(
            (attr_f64(&nvg_xml, "x") - lon).abs() <= 1e-6,
            "x=lon: {nvg_xml}"
        );
        assert!(
            (attr_f64(&nvg_xml, "y") - lat).abs() <= 1e-6,
            "y=lat: {nvg_xml}"
        );

        // 両方とも可視ダウングレードで MARK を運ぶ（trust-field が黙って消えない）。
        assert!(
            matches!(cot_out, SinkOutcome::PublishedWithDowngrade { .. }),
            "CoT must carry MARK via downgrade"
        );
        assert!(
            matches!(nvg_out, SinkOutcome::PublishedWithDowngrade { .. }),
            "NVG must carry MARK via downgrade"
        );
    }

    // 2 機種が別位置を保つ（取り違えでない＝異種 source の独立性）。
    let ComObject::PlatformState(uav_ps) = &uav_env.observation else {
        panic!("expected PlatformState")
    };
    let ComObject::PlatformState(usv_ps) = &usv_env.observation else {
        panic!("expected PlatformState")
    };
    assert_ne!(
        uav_ps.position.as_ref().unwrap().lat_deg,
        usv_ps.position.as_ref().unwrap().lat_deg,
        "two distinct platforms must retain distinct positions"
    );
}

#[test]
fn degraded_uav_shows_mark_in_both_c2_dialects() {
    // UAV-MAVLink の golden frame は time_boot 相対時刻で DEGRADED(time-suspect) になる。
    // その劣化が CoT と NVG の **両方**で可視化されることを 1 本で示す（隠さない）。
    let uav_env = MavlinkNormalizer::new()
        .normalize(&RawObservation {
            source_id: "uav-01".to_string(),
            payload: UAV_MAVLINK_FRAME.to_vec(),
            received_at: 0,
        })
        .expect("normalizes");
    assert_eq!(
        uav_env.claim.mark.status,
        musubi_types::MarkStatus::Degraded,
        "golden MAVLink frame is DEGRADED (boot-relative time)"
    );

    let cot = CotPublisher::new();
    let nvg = NvgPublisher::new();
    let report = fan_out(&uav_env, &[&cot, &nvg]);

    let cot_xml =
        String::from_utf8(report.outcome_for("cot").unwrap().bytes().unwrap().to_vec()).unwrap();
    let nvg_xml =
        String::from_utf8(report.outcome_for("nvg").unwrap().bytes().unwrap().to_vec()).unwrap();
    // 両 C2 方言とも DEGRADED + time-suspect を可視化（grep で MARK 文字列が出力にある）。
    assert!(
        cot_xml.contains("DEGRADED") && cot_xml.contains("time-suspect"),
        "{cot_xml}"
    );
    assert!(
        nvg_xml.contains("DEGRADED") && nvg_xml.contains("time-suspect"),
        "{nvg_xml}"
    );
}

/// CoT/NVG XML から数値属性を抜く（round-trip 検証用・最小）。
fn attr_f64(xml: &str, key: &str) -> f64 {
    let pat = format!("{key}=\"");
    let start = xml.find(&pat).expect("attr present") + pat.len();
    let rest = &xml[start..];
    let end = rest.find('"').expect("attr close");
    rest[..end].parse::<f64>().expect("attr parses as f64")
}
