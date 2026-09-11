//! Public synthetic contract tests: no recorded captures or external services.
use musubi_adapter_spi::recorded_jsonl::{Mapping, RecordedJsonlNormalizer, ingest};
use musubi_core::{Normalizer, RawObservation, verify_digest};
use musubi_types::{ComObject, MarkStatus, PlatformDomain};
use serde_json::{Value, json};

const RECEIVED_MS: i64 = 1_700_000_000_123;

fn profile() -> Value {
    json!({"message_type":"POS","latitude_field":"Lat","longitude_field":"Lng",
        "altitude_field":"Alt","position_units":"deg-m-msl"})
}
fn mapping(value: Value) -> Mapping {
    Mapping::parse(&serde_json::to_vec(&value).unwrap()).unwrap()
}
fn row(time: u64) -> Value {
    json!({"meta":{"type":"POS"},"data":{"TimeUS":time,"Lat":12.25,"Lng":-73.5,"Alt":45.0}})
}
fn lines(rows: &[Value]) -> Vec<u8> {
    rows.iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n")
        .into_bytes()
}

#[test]
fn every_record_is_retained_and_accounted_for_in_source_order() {
    let mut first = row(9007199254740993);
    first["extra"] = json!({"nested":[true,"synthetic",{"v":7}]});
    let mut rejected = row(8);
    rejected["data"]["Lat"] = json!(91);
    let rows = vec![
        first,
        json!({"meta":{"type":"POS"},"data":{}}),
        json!({"meta":{"type":"OTHER"},"data":{"TimeMS":4}}),
        rejected,
        row(9007199254740993),
        row(2),
    ];
    let report = ingest(
        &lines(&rows),
        &mapping(profile()),
        "synthetic-1",
        RECEIVED_MS,
    )
    .unwrap();
    assert_eq!(report.reader.records, rows);
    assert_eq!(report.untimed_indices, vec![1]);
    assert_eq!(report.unmapped_indices, vec![2]);
    assert_eq!(report.rejected.len(), 1);
    assert_eq!(report.rejected[0].record_index, 3);
    assert_eq!(report.rejected[0].error.mark.status, MarkStatus::Invalid);
    assert_eq!(
        report
            .mapped
            .iter()
            .map(|r| (r.record_index, r.boot_us))
            .collect::<Vec<_>>(),
        vec![(0, 9007199254740993), (4, 9007199254740993), (5, 2)]
    );
    let mut accounted: Vec<_> = report
        .mapped
        .iter()
        .map(|r| r.record_index)
        .chain(report.rejected.iter().map(|r| r.record_index))
        .chain(report.untimed_indices.iter().copied())
        .chain(report.unmapped_indices.iter().copied())
        .collect();
    accounted.sort_unstable();
    assert_eq!(accounted, (0..rows.len()).collect::<Vec<_>>());
    for mapped in &report.mapped {
        let ComObject::PlatformState(state) = &mapped.envelope.observation else {
            panic!("platform");
        };
        assert_eq!(state.platform_id, "synthetic-1");
        assert_eq!(state.platform_domain, PlatformDomain::Unknown);
        assert_eq!(state.timestamps.observed_at, None);
        assert_eq!(state.timestamps.received_at, RECEIVED_MS);
        let pos = state.position.as_ref().unwrap();
        assert_eq!(
            (pos.lat_deg, pos.lon_deg, pos.alt_m),
            (12.25, -73.5, Some(45.0))
        );
        assert!(mapped.envelope.content_digest.is_some());
        assert_ne!(verify_digest(&mapped.envelope).status, MarkStatus::Invalid);
        let mut changed = mapped.envelope.clone();
        changed.classification = Some("synthetic-change".into());
        assert_eq!(verify_digest(&changed).status, MarkStatus::Invalid);
    }
    let output = report.to_json();
    assert_eq!(output["records"], json!(rows));
    assert_eq!(
        output["mapped"][0]["boot_us"].as_u64(),
        Some(9007199254740993)
    );
}

#[test]
fn same_normalizer_accepts_an_alternate_profile_without_code_changes() {
    let mut p = profile();
    p["latitude_field"] = json!("north");
    p["longitude_field"] = json!("east");
    p["altitude_field"] = json!("height");
    p["domain"] = json!("surface");
    let normalizer = RecordedJsonlNormalizer::new(mapping(p));
    assert!(normalizer.manifest().is_read_only());
    let envelope = normalizer
        .normalize(&RawObservation {
            source_id: "synthetic-2".into(),
            payload: lines(&[json!({"meta":{"type":"POS"},"data":{"TimeMS":7,
            "north":-18.5,"east":62.25,"height":-4.0}})]),
            received_at: RECEIVED_MS,
        })
        .unwrap();
    let ComObject::PlatformState(state) = envelope.observation else {
        panic!("platform");
    };
    assert_eq!(state.platform_domain, PlatformDomain::Surface);
    let pos = state.position.unwrap();
    assert_eq!(
        (pos.lat_deg, pos.lon_deg, pos.alt_m),
        (-18.5, 62.25, Some(-4.0))
    );
}

#[test]
fn profile_rejects_unknown_units_keys_and_incomplete_fix_policy() {
    for (key, value) in [
        ("position_units", json!("radians")),
        ("domain", json!("guessed")),
        ("extra", json!(true)),
        ("latitude_field", json!("")),
        ("fix_field", json!("Fix")),
        ("min_fix", json!(3)),
    ] {
        let mut p = profile();
        p[key] = value;
        assert!(
            Mapping::parse(&serde_json::to_vec(&p).unwrap()).is_err(),
            "{key}"
        );
    }
    assert!(Mapping::parse(b"[]").is_err());
    assert!(Mapping::parse(&vec![b' '; 4097]).is_err());
    let trailing_sibling = format!("{},\"extra\":true", profile());
    assert!(Mapping::parse(trailing_sibling.as_bytes()).is_err());
    let mut fix_profile = profile();
    fix_profile["fix_field"] = json!("Fix");
    fix_profile["min_fix"] = json!(12);
    let valid = fix_profile.to_string();
    assert!(Mapping::parse(valid.as_bytes()).is_ok());
    for separated in ["1 2", "1\n2", "1\r2"] {
        let invalid = valid.replace("\"min_fix\":12", &format!("\"min_fix\":{separated}"));
        assert_ne!(invalid, valid);
        assert!(Mapping::parse(invalid.as_bytes()).is_err());
    }
}

#[test]
fn selected_bad_values_produce_indexed_invalid_rejections() {
    let mut p = profile();
    p["fix_field"] = json!("Fix");
    p["min_fix"] = json!(3);
    let mut good = row(1);
    good["data"]["Fix"] = json!(3);
    let mut rows = vec![good.clone()];
    for (key, value) in [
        ("Lat", json!(-91)),
        ("Lng", json!(181)),
        ("Alt", json!("45")),
        ("Fix", json!(2)),
        ("Fix", Value::Null),
        ("Lat", Value::Null),
    ] {
        let mut bad = good.clone();
        bad["data"][key] = value;
        rows.push(bad);
    }
    let report = ingest(&lines(&rows), &mapping(p), "synthetic", RECEIVED_MS).unwrap();
    assert_eq!(report.mapped.len(), 1);
    assert_eq!(
        report
            .rejected
            .iter()
            .map(|r| r.record_index)
            .collect::<Vec<_>>(),
        vec![1, 2, 3, 4, 5, 6]
    );
    assert!(
        report
            .rejected
            .iter()
            .all(|r| r.error.mark.status == MarkStatus::Invalid)
    );
    let all_bad = ingest(
        &lines(&rows[1..]),
        &mapping({
            let mut p = profile();
            p["fix_field"] = json!("Fix");
            p["min_fix"] = json!(3);
            p
        }),
        "synthetic",
        RECEIVED_MS,
    )
    .unwrap();
    assert!(all_bad.mapped.is_empty());
    assert_eq!(all_bad.rejected.len(), 6);
}

#[test]
fn malformed_or_unselected_exports_fail_and_bounds_are_enforced() {
    let m = mapping(profile());
    for bytes in [
        b"[]".as_slice(),
        b"{}",
        b"\xff",
        br#"{"meta":{"type":"OTHER"},"data":{"TimeUS":1}}"#,
        br#"{"meta":{"type":"POS"},"data":{"TimeUS":1,"TimeMS":2}}"#,
        br#"{"meta":{"type":"POS"},"data":{"TimeUS":1,"Lat":1,"Lat":2}}"#,
    ] {
        assert!(ingest(bytes, &m, "synthetic", RECEIVED_MS).is_err());
    }
    let mut oversized = row(1);
    oversized["padding"] = json!("x".repeat(65536));
    assert!(ingest(&lines(&[oversized]), &m, "synthetic", RECEIVED_MS).is_err());
    assert!(
        ingest(
            &vec![b' '; 16 * 1024 * 1024 + 1],
            &m,
            "synthetic",
            RECEIVED_MS
        )
        .is_err()
    );
    let mut deep = json!(0);
    for _ in 0..34 {
        deep = json!([deep]);
    }
    let mut record = row(1);
    record["nested"] = deep;
    assert!(ingest(&lines(&[record]), &m, "synthetic", RECEIVED_MS).is_err());
}
