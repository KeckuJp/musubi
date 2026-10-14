#![allow(clippy::expect_used)]

use musubi_cable_sim::{ScenarioKind, SensorTier, SimulationConfig, generate_ndjson};
use musubi_field_contracts::DigestV1;

const RECEIPT_JSON: &[u8] =
    include_bytes!("../../../examples/offline-verification/cable/regeneration-receipt-v1.json");

#[test]
fn committed_regeneration_receipt_matches_exact_fixture_bytes() {
    let receipt: serde_json::Value =
        serde_json::from_slice(RECEIPT_JSON).expect("regeneration receipt JSON");
    assert_eq!(receipt["watermark"], "SIMULATED");
    assert_eq!(receipt["claim_ceiling"], "SIM_SELF_ATTEST");

    let scenario = ScenarioKind::parse(
        receipt["config"]["scenario"]
            .as_str()
            .expect("scenario string"),
    )
    .expect("known scenario");
    let tier = SensorTier::parse(
        receipt["config"]["sensor_tier"]
            .as_str()
            .expect("tier string"),
    )
    .expect("known tier");
    let seed = receipt["config"]["seed"].as_u64().expect("seed");
    let tick_count = receipt["config"]["tick_count"]
        .as_u64()
        .and_then(|value| u32::try_from(value).ok())
        .expect("tick count");
    let mut config = SimulationConfig::new(scenario, tier, seed);
    config.tick_count = tick_count;

    let artifact = generate_ndjson(&config).expect("deterministic cable fixture");
    assert_eq!(
        artifact.pre_receipt_sha256,
        receipt["expected"]["pre_receipt_sha256"]
    );
    assert_eq!(
        artifact.fixture_manifest_sha256,
        receipt["expected"]["fixture_manifest_sha256"]
    );
    assert_eq!(
        artifact.public_profile_sha256,
        receipt["expected"]["public_profile_sha256"]
    );
    assert_eq!(
        artifact.public_registry_sha256,
        receipt["expected"]["public_registry_sha256"]
    );
    assert_eq!(
        artifact.pre_receipt_record_count as u64,
        receipt["expected"]["pre_receipt_record_count"]
    );
    assert_eq!(
        DigestV1::sha256(&artifact.bytes).as_str(),
        receipt["expected"]["complete_ndjson_sha256"]
    );

    let lines: Vec<serde_json::Value> = std::str::from_utf8(&artifact.bytes)
        .expect("UTF-8")
        .lines()
        .map(|line| serde_json::from_str(line).expect("record JSON"))
        .collect();
    assert_eq!(
        lines.len() as u64,
        receipt["expected"]["complete_record_count"]
    );
    assert_eq!(
        lines[0]["fixture_manifest_sha256"],
        receipt["expected"]["fixture_manifest_sha256"]
    );
    assert_eq!(
        lines.last().expect("receipt")["fixture_manifest_sha256"],
        receipt["expected"]["fixture_manifest_sha256"]
    );
    assert_eq!(
        lines[0]["transformer_configuration_sha256"],
        receipt["expected"]["transformer_configuration_sha256"]
    );
    assert_eq!(
        lines.last().expect("receipt")["pre_receipt_byte_count"],
        receipt["expected"]["pre_receipt_byte_count"]
    );
}
