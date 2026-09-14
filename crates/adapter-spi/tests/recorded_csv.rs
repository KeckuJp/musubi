//! Synthetic inputs exercise the public CSV-to-COM contract without recorded captures.
use musubi_adapter_spi::recorded_csv::{IngestReport, ingest_boot_csv};
use musubi_adapter_spi::recorded_jsonl::Mapping;
use musubi_core::verify_digest;
use musubi_decoded_csv::{ParseOptions, Value as Cell};
use musubi_types::{ComObject, ConfidenceBasis, MarkStatus, PlatformDomain};
use serde_json::{Value, json};
use std::fmt::Write;

const RECEIVED_MS: i64 = 1_700_000_000_123;

fn profile() -> Value {
    json!({"message_type":"POS","latitude_field":"Lat","longitude_field":"Lng",
        "altitude_field":"Alt","position_units":"deg-m-msl"})
}

fn mapping(profile: Value) -> Mapping {
    Mapping::parse(&serde_json::to_vec(&profile).unwrap()).unwrap()
}

fn ingest(input: &str, options: ParseOptions) -> IngestReport {
    ingest_boot_csv(
        input.as_bytes(),
        "Tick",
        &mapping(profile()),
        "synthetic-csv",
        RECEIVED_MS,
        options,
    )
    .unwrap()
}

fn assert_accounted(report: &IngestReport) {
    let mut indices: Vec<_> = report
        .mapped
        .iter()
        .map(|row| row.record_index)
        .chain(report.rejected.iter().map(|row| row.record_index))
        .collect();
    indices.sort_unstable();
    assert_eq!(indices, (0..report.table.rows.len()).collect::<Vec<_>>());
    for rejected in &report.rejected {
        assert_eq!(rejected.error.mark.status, MarkStatus::Invalid);
        assert_eq!(rejected.error.source_id, "synthetic-csv");
        assert!(!rejected.error.mark.reason_code.is_empty());
    }
}

#[test]
fn retains_unknown_values_and_exact_time_while_sealing_only_declared_com() {
    let report = ingest(
        "Tick (us),Lat (deg),Lng (deg),Alt (m),Opaque (counts),Label,Empty\n\
         9007199254740993,12.25,-73.5,45,9223372036854775808,synthetic-A,\n\
         9007199254740994,12.25,-73.5,45,7,synthetic-B,\n",
        ParseOptions::default(),
    );
    assert_accounted(&report);
    assert_eq!(report.mapped.len(), 2);
    assert_eq!(report.table.columns[4].name, "Opaque");
    assert_eq!(report.table.columns[4].unit.as_deref(), Some("counts"));
    assert_eq!(
        report.table.rows[0].values[4],
        Cell::Text("9223372036854775808".into())
    );
    assert_eq!(
        report.table.rows[0].values[5],
        Cell::Text("synthetic-A".into())
    );
    assert_eq!(report.table.rows[0].values[6], Cell::Blank);
    assert_eq!(report.table.rows[1].values[4], Cell::Integer(7));
    for (index, mapped) in report.mapped.iter().enumerate() {
        assert_eq!(mapped.record_index, index);
        assert_eq!(mapped.boot_us, 9_007_199_254_740_993 + index as u64);
        assert_eq!(report.table.rows[index].boot_us, mapped.boot_us);
        assert_eq!(report.table.rows[index].source_line, index + 2);
        let envelope = &mapped.envelope;
        let ComObject::PlatformState(state) = &envelope.observation else {
            panic!("expected platform state");
        };
        let position = state.position.as_ref().unwrap();
        assert_eq!(state.platform_id, "synthetic-csv");
        assert_eq!(state.platform_domain, PlatformDomain::Unknown);
        assert_eq!(state.timestamps.observed_at, None);
        assert_eq!(state.timestamps.received_at, RECEIVED_MS);
        assert_eq!(state.timestamps.time_confidence, 0.0);
        assert_eq!(envelope.claim.confidence, 0.0);
        assert_eq!(
            envelope.claim.confidence_basis,
            Some(ConfidenceBasis::AdapterAssigned)
        );
        assert_eq!(
            (position.lat_deg, position.lon_deg, position.alt_m),
            (12.25, -73.5, Some(45.0))
        );
        assert!(envelope.content_digest.is_some());
        assert_ne!(verify_digest(envelope).status, MarkStatus::Invalid);
        assert!(envelope.signature.is_none());
        let mut changed = envelope.clone();
        changed.claim.confidence = 0.5;
        assert_eq!(verify_digest(&changed).status, MarkStatus::Invalid);
    }
    // Changing an unsealed source column does not manufacture a different COM observation.
    assert_eq!(
        report.mapped[0].envelope.content_digest,
        report.mapped[1].envelope.content_digest
    );
}

#[test]
fn alternate_columns_and_explicit_domain_use_the_same_connection() {
    let mut alternate = profile();
    alternate["latitude_field"] = json!("north");
    alternate["longitude_field"] = json!("east");
    alternate["altitude_field"] = json!("height");
    alternate["domain"] = json!("surface");
    alternate["source_channel_path"] = json!(["data", "Channel"]);
    let report = ingest_boot_csv(
        b"Clock,north,east,height,Channel\n0,-18.5,62.25,-4,receiver-A\n1,-18.5,62.25,-4,receiver-B\n",
        "Clock",
        &mapping(alternate),
        "synthetic-csv",
        RECEIVED_MS,
        ParseOptions::default(),
    ).unwrap();
    assert_accounted(&report);
    assert_eq!(report.mapped.len(), 2);
    let ComObject::PlatformState(state) = &report.mapped[0].envelope.observation else {
        panic!("expected platform state");
    };
    assert_eq!(state.platform_domain, PlatformDomain::Surface);
    let position = state.position.as_ref().unwrap();
    assert_eq!(
        (position.lat_deg, position.lon_deg, position.alt_m),
        (-18.5, 62.25, Some(-4.0))
    );
    for (index, label) in ["receiver-A", "receiver-B"].iter().enumerate() {
        let envelope = &report.mapped[index].envelope;
        assert!(
            envelope
                .claim
                .mark
                .provenance
                .contains(&format!("recorded-channel:{label}"))
        );
        assert!(
            envelope
                .claim
                .mark
                .provenance
                .contains(&"channel-identity:source-asserted".into())
        );
        assert_ne!(verify_digest(envelope).status, MarkStatus::Invalid);
    }
    assert_ne!(
        report.mapped[0].envelope.content_digest,
        report.mapped[1].envelope.content_digest
    );
    let mut declared_label = profile();
    declared_label["source_channel_path"] = json!(["meta", "type"]);
    let error = ingest_boot_csv(
        b"Tick,Lat,Lng,Alt\n0,1,2,3\n",
        "Tick",
        &mapping(declared_label),
        "synthetic-csv",
        RECEIVED_MS,
        ParseOptions::default(),
    )
    .unwrap_err();
    assert_eq!(error.mark.status, MarkStatus::Invalid);
}

#[test]
fn selected_missing_text_nonfinite_and_out_of_range_values_are_indexed_invalid() {
    let report = ingest(
        "Tick,Lat,Lng,Alt\n0,0,0,0\n1,,0,0\n2,0,text,0\n3,0,0,NaN\n\
         4,91,0,0\n5,0,-181,0\n6,0,0,\n7,inf,0,0\n8,0,-inf,0\n",
        ParseOptions {
            preserve_nonfinite_as_text: true,
            ..ParseOptions::default()
        },
    );
    assert_accounted(&report);
    assert_eq!(report.mapped.len(), 1);
    assert_eq!(report.mapped[0].record_index, 0);
    assert_eq!(
        report
            .rejected
            .iter()
            .map(|r| r.record_index)
            .collect::<Vec<_>>(),
        (1..9).collect::<Vec<_>>()
    );
    assert_eq!(report.table.rows[3].values[3], Cell::Text("NaN".into()));
    let all_invalid = ingest("Tick,Lat,Lng,Alt\n0,,,\n", ParseOptions::default());
    assert_accounted(&all_invalid);
    assert!(all_invalid.mapped.is_empty());
    assert_eq!(all_invalid.rejected.len(), 1);
}

#[test]
fn unknown_nonfinite_tokens_require_opt_in_and_remain_text() {
    let input = "Tick,Lat,Lng,Alt,Unknown\n0,1,2,3,NaN\n1,1,2,3,inf\n2,1,2,3,-inf\n";
    assert!(
        ingest_boot_csv(
            input.as_bytes(),
            "Tick",
            &mapping(profile()),
            "synthetic-csv",
            RECEIVED_MS,
            ParseOptions::default()
        )
        .is_err()
    );
    let report = ingest(
        input,
        ParseOptions {
            preserve_nonfinite_as_text: true,
            ..ParseOptions::default()
        },
    );
    assert_accounted(&report);
    assert_eq!(report.mapped.len(), 3);
    for (index, token) in ["NaN", "inf", "-inf"].iter().enumerate() {
        assert_eq!(
            report.table.rows[index].values[4],
            Cell::Text((*token).into())
        );
    }
}

#[test]
fn equal_time_requires_opt_in_and_preserves_source_indices() {
    let input = "Tick,Lat,Lng,Alt\n7,1,2,3\n7,4,5,6\n8,7,8,9\n";
    assert!(
        ingest_boot_csv(
            input.as_bytes(),
            "Tick",
            &mapping(profile()),
            "synthetic-csv",
            RECEIVED_MS,
            ParseOptions::default()
        )
        .is_err()
    );
    let report = ingest(
        input,
        ParseOptions {
            allow_equal_time: true,
            ..ParseOptions::default()
        },
    );
    assert_accounted(&report);
    assert_eq!(
        report
            .mapped
            .iter()
            .map(|r| (r.record_index, r.boot_us))
            .collect::<Vec<_>>(),
        vec![(0, 7), (1, 7), (2, 8)]
    );
}

#[test]
fn malformed_structure_time_and_declared_units_abort_atomically() {
    let cases = [
        "Tick,Lat,Lng,Alt,Lat\n0,1,2,3,4\n",
        "Tick,Lat,Lng,Alt\n0,1,2,3\n1,1,2\n",
        "Tick,Lat,Lng,Alt\n2,1,2,3\n1,1,2,3\n",
        "Tick,Lat,Lng,Alt\n0,1,2,3\n-1,1,2,3\n",
        "Tick,Lat,Lng,Alt\n0,1,2,3\n1.5,1,2,3\n",
        "Tick,Lat,Lng,Alt\n0,1,2,3\nNaN,1,2,3\n",
        "Tick,Lat,Lng,Alt\n0,1,2,3\n,1,2,3\n",
        "Tick (ms),Lat,Lng,Alt\n0,1,2,3\n",
        "Tick,Lat (rad),Lng,Alt\n0,1,2,3\n",
        "Tick,Lat,Lng (rad),Alt\n0,1,2,3\n",
        "Tick,Lat,Lng,Alt (ft)\n0,1,2,3\n",
    ];
    for input in cases {
        for options in [
            ParseOptions::default(),
            ParseOptions {
                allow_equal_time: true,
                preserve_nonfinite_as_text: true,
            },
        ] {
            let error = ingest_boot_csv(
                input.as_bytes(),
                "Tick",
                &mapping(profile()),
                "synthetic-csv",
                RECEIVED_MS,
                options,
            )
            .unwrap_err();
            assert_eq!(error.mark.status, MarkStatus::Invalid);
            assert_eq!(error.source_id, "synthetic-csv");
        }
    }
}

#[test]
fn fix_and_source_channel_failures_are_not_fallback_values() {
    let mut selected = profile();
    selected["fix_field"] = json!("Fix");
    selected["min_fix"] = json!(3);
    selected["source_channel_path"] = json!(["data", "Channel"]);
    let report = ingest_boot_csv(
        b"Tick,Lat,Lng,Alt,Fix,Channel\n0,1,2,3,3,receiver-A\n1,1,2,3,2,receiver-A\n2,1,2,3,,receiver-A\n3,1,2,3,3,7\n4,1,2,3,3,\n",
        "Tick", &mapping(selected), "synthetic-csv", RECEIVED_MS, ParseOptions::default(),
    ).unwrap();
    assert_accounted(&report);
    assert_eq!(report.mapped.len(), 1);
    assert_eq!(
        report
            .rejected
            .iter()
            .map(|r| r.record_index)
            .collect::<Vec<_>>(),
        vec![1, 2, 3, 4]
    );
}

#[test]
fn thirty_thousand_rows_have_one_outcome_each_without_a_small_record_cap() {
    let mut input = String::from("Tick,Lat,Lng,Alt,Unknown\n");
    for index in 0..30_000 {
        let latitude = if index % 7 == 0 { "" } else { "12.25" };
        writeln!(input, "{index},{latitude},-73.5,45,synthetic-{index}").unwrap();
    }
    let report = ingest(&input, ParseOptions::default());
    assert_eq!(report.table.rows.len(), 30_000);
    assert_accounted(&report);
    assert_eq!(
        report.rejected.len(),
        (0..30_000).filter(|index| index % 7 == 0).count()
    );
    assert_eq!(report.mapped.len() + report.rejected.len(), 30_000);
    assert_eq!(report.table.rows.last().unwrap().source_line, 30_001);
    assert_eq!(
        report.table.rows.last().unwrap().values[4],
        Cell::Text("synthetic-29999".into())
    );
}

#[test]
fn input_line_and_row_capacity_limits_fail_without_partial_output() {
    let header = "Tick,Lat,Lng,Alt,Unknown\n";
    let maximum_line = format!("0,1,2,3,{}", "x".repeat(64 * 1024 - 8));
    assert_eq!(maximum_line.len(), 64 * 1024);
    let at_limit = ingest(
        &format!("{header}{maximum_line}\n"),
        ParseOptions::default(),
    );
    assert_eq!(at_limit.mapped.len(), 1);
    let overline = format!("{header}{maximum_line}x\n");
    let too_many_rows = format!("Tick,Lat,Lng,Alt\n{}", "0,,,\n".repeat(100_001));
    let over_input = vec![b' '; 16 * 1024 * 1024 + 1];
    for (bytes, reason) in [
        (overline.as_bytes(), "csv-line-or-row-bound"),
        (too_many_rows.as_bytes(), "csv-line-or-row-bound"),
        (over_input.as_slice(), "input-oversize"),
    ] {
        let error = ingest_boot_csv(
            bytes,
            "Tick",
            &mapping(profile()),
            "synthetic-csv",
            RECEIVED_MS,
            ParseOptions {
                allow_equal_time: true,
                ..ParseOptions::default()
            },
        )
        .unwrap_err();
        assert_eq!(error.mark.status, MarkStatus::Invalid);
        assert_eq!(error.source_id, "synthetic-csv");
        assert_eq!(error.mark.reason_code, reason);
    }
}
