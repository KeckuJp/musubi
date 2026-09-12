//! Authored synthetic channel provenance contracts. No captures or external services.
use musubi_adapter_spi::recorded_jsonl::{Mapping, ingest};
use musubi_core::verify_digest;
use musubi_types::{ComObject, MarkStatus, PlatformDomain};
use serde_json::{Value, json};

const RECEIVED_MS: i64 = 1_700_000_000_123;

fn profile() -> Value {
    json!({"message_type":"POS","latitude_field":"Lat","longitude_field":"Lng",
        "altitude_field":"Alt","position_units":"deg-m-msl"})
}
fn channel_profile() -> Value {
    let mut value = profile();
    value["source_channel_path"] = json!(["data", "source_record", "I"]);
    value
}
fn mapping(value: Value) -> Mapping {
    Mapping::parse(&serde_json::to_vec(&value).unwrap()).unwrap()
}
fn row(channel: Value) -> Value {
    json!({"meta":{"type":"POS"},"data":{"TimeUS":7,"Lat":12.25,"Lng":-73.5,
        "Alt":45.0,"source_record":{"I":channel,"unknown":{"nested":[true,"retained",7]}}}})
}
fn lines(rows: &[Value]) -> Vec<u8> {
    rows.iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n")
        .into_bytes()
}

#[test]
fn declared_labels_are_bound_without_changing_vehicle_time_or_confidence() {
    let labels = ["0", "00", "1", "gps-A.02", "a_"];
    let rows: Vec<_> = labels.iter().map(|label| row(json!(label))).collect();
    let report = ingest(
        &lines(&rows),
        &mapping(channel_profile()),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    assert_eq!(report.reader.records, rows);
    assert_eq!(report.mapped.len(), labels.len());
    assert!(report.rejected.is_empty());
    let baseline = ingest(
        &lines(&[row(json!("ignored"))]),
        &mapping(profile()),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    for (index, mapped) in report.mapped.iter().enumerate() {
        assert_eq!((mapped.record_index, mapped.boot_us), (index, 7));
        let envelope = &mapped.envelope;
        let ComObject::PlatformState(state) = &envelope.observation else {
            panic!("platform expected")
        };
        assert_eq!(state.platform_id, "synthetic-vehicle");
        assert_eq!(state.platform_domain, PlatformDomain::Unknown);
        assert_eq!(state.timestamps.observed_at, None);
        assert_eq!(state.timestamps.received_at, RECEIVED_MS);
        assert_eq!(state.timestamps.time_confidence, 0.0);
        assert_eq!(envelope.claim.confidence, 0.0);
        assert_eq!(
            envelope.claim.confidence_basis,
            baseline.mapped[0].envelope.claim.confidence_basis
        );
        assert_eq!(
            envelope.claim.mark.status,
            baseline.mapped[0].envelope.claim.mark.status
        );
        let position = state.position.as_ref().unwrap();
        assert_eq!(
            (position.lat_deg, position.lon_deg, position.alt_m),
            (12.25, -73.5, Some(45.0))
        );
        let mut expected = baseline.mapped[0].envelope.claim.mark.provenance.clone();
        expected.push(format!("recorded-channel:{}", labels[index]));
        expected.push("channel-identity:source-asserted".into());
        assert_eq!(envelope.claim.mark.provenance, expected);
        assert_ne!(verify_digest(envelope).status, MarkStatus::Invalid);
        for earlier in &report.mapped[..index] {
            assert_ne!(envelope.content_digest, earlier.envelope.content_digest);
        }
    }
    let output = report.to_json();
    assert_eq!(output["records"], json!(rows));
    assert_eq!(output["source_records_sealed"], false);
    assert_eq!(
        output["mapped"][1]["provenance"],
        json!(report.mapped[1].envelope.claim.mark.provenance)
    );
}

#[test]
fn channel_annotation_change_or_removal_invalidates_digest() {
    let report = ingest(
        &lines(&[row(json!("receiver-A"))]),
        &mapping(channel_profile()),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    let sealed = &report.mapped[0].envelope;
    let mut changed = sealed.clone();
    let index = changed
        .claim
        .mark
        .provenance
        .iter()
        .position(|p| p == "recorded-channel:receiver-A")
        .unwrap();
    changed.claim.mark.provenance[index] = "recorded-channel:receiver-B".into();
    assert_eq!(verify_digest(&changed).status, MarkStatus::Invalid);
    let mut removed = sealed.clone();
    removed
        .claim
        .mark
        .provenance
        .retain(|p| p != "channel-identity:source-asserted");
    assert_eq!(verify_digest(&removed).status, MarkStatus::Invalid);
}

#[test]
fn absent_option_keeps_old_provenance_and_digest_despite_sidecar_changes() {
    let mut absent = row(json!("0"));
    absent["data"]
        .as_object_mut()
        .unwrap()
        .remove("source_record");
    let report = ingest(
        &lines(&[
            absent,
            row(json!("0")),
            row(json!(42)),
            row(json!("invalid;label")),
        ]),
        &mapping(profile()),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    assert_eq!(report.mapped.len(), 4);
    assert!(report.rejected.is_empty());
    for mapped in &report.mapped {
        assert_eq!(
            mapped.envelope.content_digest,
            report.mapped[0].envelope.content_digest
        );
        assert_eq!(
            mapped.envelope.claim.mark.provenance,
            report.mapped[0].envelope.claim.mark.provenance
        );
        assert!(
            !mapped
                .envelope
                .claim
                .mark
                .provenance
                .iter()
                .any(|p| p.starts_with("recorded-channel:") || p.starts_with("channel-identity:"))
        );
    }
}

#[test]
fn malformed_selectors_are_rejected_as_profiles() {
    for path in [
        Value::Null,
        json!("data.source_record.I"),
        json!([]),
        json!([1]),
        json!([true]),
        json!([""]),
        json!(["a;b"]),
        json!(["control\nkey"]),
        json!(["x".repeat(257)]),
        json!(["a", "b", "c", "d", "e", "f", "g", "h", "i"]),
    ] {
        let mut value = profile();
        value["source_channel_path"] = path;
        assert!(Mapping::parse(&serde_json::to_vec(&value).unwrap()).is_err());
    }
}

#[test]
fn invalid_or_missing_channel_is_an_indexed_rejection_not_a_fallback() {
    let bad = [
        json!(0),
        json!(0.0),
        json!(true),
        Value::Null,
        json!([]),
        json!({"I":"0"}),
        json!(""),
        json!("a".repeat(65)),
        json!("a b"),
        json!("trim "),
        json!("a;b"),
        json!("a:b"),
        json!("a/b"),
        json!("*"),
        json!("é"),
        json!("a\nb"),
    ];
    let mut rows = vec![row(json!("good"))];
    rows.extend(bad.into_iter().map(row));
    let mut missing = row(json!("good"));
    missing["data"]["source_record"]
        .as_object_mut()
        .unwrap()
        .remove("I");
    rows.push(missing);
    let mut wrong_parent = row(json!("good"));
    wrong_parent["data"]["source_record"] = json!([{"I":"good"}]);
    rows.push(wrong_parent);
    let report = ingest(
        &lines(&rows),
        &mapping(channel_profile()),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    assert_eq!(report.reader.records, rows);
    assert_eq!(report.mapped.len(), 1);
    assert_eq!(report.mapped[0].record_index, 0);
    assert_eq!(
        report
            .rejected
            .iter()
            .map(|r| r.record_index)
            .collect::<Vec<_>>(),
        (1..rows.len()).collect::<Vec<_>>()
    );
    assert!(
        report
            .rejected
            .iter()
            .all(|r| r.error.mark.status == MarkStatus::Invalid)
    );
}

#[test]
fn literal_keys_are_not_dotted_paths_and_eight_key_depth_is_supported() {
    let mut p = profile();
    p["source_channel_path"] = json!(["data", "literal.I"]);
    let mut record = row(json!("wrong"));
    record["data"]["literal.I"] = json!("Exact_01");
    record["data"]["literal"] = json!({"I":"not-selected"});
    let report = ingest(
        &lines(&[record]),
        &mapping(p),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    assert!(
        report.mapped[0]
            .envelope
            .claim
            .mark
            .provenance
            .contains(&"recorded-channel:Exact_01".into())
    );
    let mut p = profile();
    p["source_channel_path"] = json!(["data", "a", "b", "c", "d", "e", "f", "g"]);
    let mut record = row(json!("not-selected"));
    record["data"]["a"] = json!({"b":{"c":{"d":{"e":{"f":{"g":"Z".repeat(64)}}}}}});
    let report = ingest(
        &lines(&[record]),
        &mapping(p),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    assert!(
        report.mapped[0]
            .envelope
            .claim
            .mark
            .provenance
            .contains(&format!("recorded-channel:{}", "Z".repeat(64)))
    );
}

#[test]
fn unselected_and_untimed_records_keep_their_original_accounting() {
    let mut untimed = row(Value::Null);
    untimed["data"].as_object_mut().unwrap().remove("TimeUS");
    let mut other = row(Value::Null);
    other["meta"]["type"] = json!("OTHER");
    let rows = vec![
        row(json!("0")),
        untimed,
        other,
        row(Value::Null),
        row(json!("1")),
        row(json!("0")),
    ];
    let report = ingest(
        &lines(&rows),
        &mapping(channel_profile()),
        "synthetic-vehicle",
        RECEIVED_MS,
    )
    .unwrap();
    assert_eq!(report.reader.records, rows);
    assert_eq!(report.untimed_indices, vec![1]);
    assert_eq!(report.unmapped_indices, vec![2]);
    assert_eq!(
        report
            .rejected
            .iter()
            .map(|r| r.record_index)
            .collect::<Vec<_>>(),
        vec![3]
    );
    assert_eq!(
        report
            .mapped
            .iter()
            .map(|r| (r.record_index, r.boot_us))
            .collect::<Vec<_>>(),
        vec![(0, 7), (4, 7), (5, 7)]
    );
    assert_eq!(
        report.mapped[0].envelope.content_digest,
        report.mapped[2].envelope.content_digest
    );
    assert_eq!(
        report.mapped.len()
            + report.rejected.len()
            + report.untimed_indices.len()
            + report.unmapped_indices.len(),
        rows.len()
    );
}
