#![allow(clippy::expect_used)]

use crate::report::{MAX_REPORT_BYTES, load, parse};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

const SOURCE: &[u8] = b"synthetic sample\n";
static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn document() -> Value {
    json!({
        "schema": "recorded-observation-report/v1", "com_sealed": false,
        "source_id": "synthetic-source", "received_at_ms": 1,
        "input_sha256": digest(SOURCE), "profile_sha256": "a".repeat(64),
        "meanings_sha256": "b".repeat(64), "source_bytes": SOURCE.len(),
        "source_hex": "73796e7468657469632073616d706c650a",
        "source_record_count": null,
        "accounting": {"source_record_count": "not-inferred-from-observation-count"},
        "observation_count": 1,
        "observations": [{
            "observation_index": 0, "source": "synthetic-source",
            "source_role": "recorded_export", "channel": "event",
            "t_ms": -17, "clock_basis": "unknown", "time_confidence": 0.0,
            "t_boot_us": null, "wall_ms": null, "anchor_unix_us": null,
            "stale": false,
            "subject": {"kind": "synthetic", "frame": "unknown", "convention": "declared"},
            "fields": [
                {"name": "unregistered_z", "value": "nonfinite_nan", "source_unit": null,
                 "meaning": null, "unit": null, "basis": null, "disposition": "nonfinite-as-text"},
                {"name": "unregistered_a", "value": null, "source_unit": null,
                 "meaning": null, "unit": null, "basis": null, "disposition": "blank"}
            ]
        }]
    })
}

fn bytes(value: &Value) -> Vec<u8> {
    serde_json::to_vec(value).expect("synthetic JSON serializes")
}

fn rejected(value: &Value) {
    let result = parse(&bytes(value));
    assert!(result.is_err(), "invalid report must be rejected");
    assert!(!result.err().expect("error").trim().is_empty());
}

struct TestDirectory(PathBuf);

impl TestDirectory {
    fn new() -> Self {
        loop {
            let serial = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "musubi-report-test-{}-{serial}",
                std::process::id()
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => panic!("cannot create synthetic directory: {error}"),
            }
        }
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn report_preserves_unknowns_nulls_text_and_file_order_without_clock_inference() {
    let mut expected = document();
    let mut second = expected["observations"][0].clone();
    second["observation_index"] = json!(1);
    second["t_ms"] = json!(-30);
    second["fields"][0]["value"] = json!("007.50");
    second["fields"][0]["disposition"] = json!("present");
    expected["observations"]
        .as_array_mut()
        .expect("array")
        .push(second);
    expected["observation_count"] = json!(2);
    let actual = parse(&bytes(&expected)).expect("valid report");
    assert_eq!(actual.document, expected);
    assert_eq!(actual.document["observations"][0]["wall_ms"], Value::Null);
    assert_eq!(actual.document["observations"][1]["t_ms"], -30);
    assert_eq!(
        actual.document["observations"][1]["fields"][0]["value"],
        "007.50"
    );
}

#[test]
fn report_accepts_zero_observations_without_fabricating_a_record() {
    let mut expected = document();
    expected["observations"] = json!([]);
    expected["observation_count"] = json!(0);
    let actual = parse(&bytes(&expected)).expect("empty report is explicit");
    assert_eq!(actual.document, expected);
}

#[test]
fn report_hash_covers_original_report_bytes_and_is_not_embedded_source_hash() {
    let expected = document();
    let compact = bytes(&expected);
    let mut spaced = b" \n\t".to_vec();
    spaced.extend_from_slice(&compact);
    spaced.extend_from_slice(b"\r\n");
    let first = parse(&compact).expect("compact report");
    let second = parse(&spaced).expect("whitespace report");
    assert_eq!(first.document, second.document);
    assert_eq!(first.file_sha256, digest(&compact));
    assert_eq!(second.file_sha256, digest(&spaced));
    assert_ne!(first.file_sha256, second.file_sha256);
    assert_ne!(first.file_sha256, digest(SOURCE));
}

#[test]
fn report_preserves_format_accounting_and_retained_source_records() {
    let mut expected = document();
    expected["source_record_count"] = json!(7);
    expected["accounting"] = json!({"untimed_records": 6, "retained_records": 7});
    expected["value_basis"] = json!("pymavlink_decoded_no_second_wire_scaling");
    expected["source_records"] = json!([{"kind": "synthetic", "value": "unknown"}]);
    assert_eq!(
        parse(&bytes(&expected)).expect("format metadata").document,
        expected
    );
}

#[test]
fn report_rejects_malformed_json_trailing_documents_and_non_objects() {
    for input in [b"{".as_slice(), b"null", b"[]", b"{} {}", b"\xff"] {
        assert!(parse(input).is_err());
    }
    let mut input = bytes(&document());
    input.extend_from_slice(b" {}");
    assert!(parse(&input).is_err());
}

#[test]
fn report_rejects_duplicate_keys_at_root_and_nested_depths() {
    let encoded = String::from_utf8(bytes(&document())).expect("UTF-8");
    for (needle, replacement) in [
        (
            "\"com_sealed\":false",
            "\"com_sealed\":true,\"com_sealed\":false",
        ),
        ("\"t_ms\":-17", "\"t_ms\":999,\"t_ms\":-17"),
        (
            "\"name\":\"unregistered_z\"",
            "\"name\":\"hidden\",\"name\":\"unregistered_z\"",
        ),
    ] {
        assert!(encoded.contains(needle));
        assert!(parse(encoded.replacen(needle, replacement, 1).as_bytes()).is_err());
    }
}

#[test]
fn report_rejects_wrong_schema_or_sealed_claim() {
    for (key, value) in [
        ("schema", json!("recorded-observation-report/v2")),
        ("com_sealed", json!(true)),
        ("com_sealed", json!("false")),
    ] {
        let mut invalid = document();
        invalid[key] = value;
        rejected(&invalid);
    }
}

#[test]
fn report_rejects_missing_required_root_fields() {
    for key in [
        "schema",
        "com_sealed",
        "source_id",
        "received_at_ms",
        "input_sha256",
        "profile_sha256",
        "meanings_sha256",
        "source_bytes",
        "source_hex",
        "source_record_count",
        "accounting",
        "observation_count",
        "observations",
    ] {
        let mut invalid = document();
        invalid.as_object_mut().expect("object").remove(key);
        rejected(&invalid);
    }
}

#[test]
fn report_rejects_mismatched_counts_and_declared_bounds() {
    for (key, value) in [
        ("observation_count", json!(0)),
        ("observation_count", json!(100_001)),
        ("observation_count", json!(-1)),
        ("observation_count", json!(1.5)),
        ("source_bytes", json!(SOURCE.len() + 1)),
        ("source_bytes", json!(16 * 1024 * 1024 + 1)),
        ("source_record_count", json!(-1)),
    ] {
        let mut invalid = document();
        invalid[key] = value;
        rejected(&invalid);
    }
}

#[test]
fn report_rejects_bad_source_encoding_and_source_digest_mismatch() {
    for value in ["0", "xx", "00", "73796e7468657469632073616d706c6500"] {
        let mut invalid = document();
        invalid["source_hex"] = json!(value);
        rejected(&invalid);
    }
    let mut invalid = document();
    invalid["input_sha256"] = json!("0".repeat(64));
    rejected(&invalid);
}

#[test]
fn report_checks_digest_syntax_without_claiming_unavailable_profile_bytes() {
    for key in ["input_sha256", "profile_sha256", "meanings_sha256"] {
        for value in [json!("a".repeat(63)), json!("g".repeat(64)), Value::Null] {
            let mut invalid = document();
            invalid[key] = value;
            rejected(&invalid);
        }
    }
    let mut expected = document();
    expected["profile_sha256"] = json!("c".repeat(64));
    expected["meanings_sha256"] = json!("d".repeat(64));
    assert_eq!(
        parse(&bytes(&expected))
            .expect("unavailable bytes are not invented")
            .document,
        expected
    );
}

#[test]
fn report_rejects_missing_required_row_fields() {
    for key in [
        "observation_index",
        "source",
        "source_role",
        "channel",
        "t_ms",
        "clock_basis",
        "time_confidence",
        "t_boot_us",
        "wall_ms",
        "anchor_unix_us",
        "stale",
        "subject",
        "fields",
    ] {
        let mut invalid = document();
        invalid["observations"][0]
            .as_object_mut()
            .expect("row")
            .remove(key);
        rejected(&invalid);
    }
}

#[test]
fn report_rejects_invalid_row_types_and_indices() {
    for (key, value) in [
        ("observation_index", json!(1)),
        ("source", Value::Null),
        ("source_role", json!(4)),
        ("channel", json!([])),
        ("t_ms", json!("-17")),
        ("clock_basis", Value::Null),
        ("time_confidence", json!("unknown")),
        ("t_boot_us", json!(-1)),
        ("wall_ms", json!(1.25)),
        ("anchor_unix_us", json!("0")),
        ("stale", json!(0)),
        ("subject", json!([])),
        ("fields", json!({})),
    ] {
        let mut invalid = document();
        invalid["observations"][0][key] = value;
        rejected(&invalid);
    }
}

#[test]
fn report_rejects_missing_or_bad_field_shapes() {
    for key in [
        "name",
        "value",
        "source_unit",
        "meaning",
        "unit",
        "basis",
        "disposition",
    ] {
        let mut invalid = document();
        invalid["observations"][0]["fields"][0]
            .as_object_mut()
            .expect("field")
            .remove(key);
        rejected(&invalid);
    }
    for (key, value) in [
        ("name", Value::Null),
        ("value", json!({"nested": 1})),
        ("value", json!(true)),
        ("source_unit", json!(1)),
        ("meaning", json!(1)),
        ("unit", json!(false)),
        ("basis", json!([])),
        ("disposition", Value::Null),
    ] {
        let mut invalid = document();
        invalid["observations"][0]["fields"][0][key] = value;
        rejected(&invalid);
    }
}

#[test]
fn report_rejects_row_source_that_does_not_match_embedded_source_context() {
    let mut invalid = document();
    invalid["observations"][0]["source"] = json!("different-synthetic-source");
    rejected(&invalid);
}

#[test]
fn report_requires_complete_declared_subject_context() {
    for key in ["kind", "frame", "convention"] {
        let mut invalid = document();
        invalid["observations"][0]["subject"]
            .as_object_mut()
            .expect("subject")
            .remove(key);
        rejected(&invalid);
        for value in [
            json!(""),
            json!("control\ncharacter"),
            json!("x".repeat(257)),
            json!(1),
        ] {
            let mut invalid = document();
            invalid["observations"][0]["subject"][key] = value;
            rejected(&invalid);
        }
    }
}

#[test]
fn report_meaning_unit_and_basis_are_all_declared_or_all_null() {
    for key in ["meaning", "unit", "basis"] {
        let mut invalid = document();
        invalid["observations"][0]["fields"][0][key] = json!("declared");
        rejected(&invalid);
    }
    let mut expected = document();
    let field = &mut expected["observations"][0]["fields"][0];
    field["meaning"] = json!("synthetic_scalar");
    field["unit"] = json!("V");
    field["basis"] = json!("declared");
    assert_eq!(
        parse(&bytes(&expected))
            .expect("complete declaration")
            .document,
        expected
    );
    for key in ["meaning", "unit", "basis"] {
        let mut invalid = expected.clone();
        invalid["observations"][0]["fields"][0][key] = Value::Null;
        rejected(&invalid);
    }
}

#[test]
fn report_loads_regular_file_and_rejects_missing_or_directory() {
    let directory = TestDirectory::new();
    let path = directory.0.join("synthetic.json");
    let expected = document();
    fs::write(&path, bytes(&expected)).expect("write synthetic report");
    assert_eq!(load(&path).expect("regular file").document, expected);
    assert!(load(&directory.0).is_err());
    assert!(load(&directory.0.join("missing.json")).is_err());
}

#[test]
fn report_load_rejects_oversize_sparse_file_before_reading() {
    let directory = TestDirectory::new();
    let path = directory.0.join("oversize.json");
    let file = fs::File::create(&path).expect("create sparse file");
    file.set_len(MAX_REPORT_BYTES as u64 + 1)
        .expect("set sparse size");
    assert!(load(&path).is_err());
}

#[test]
fn report_parse_rejects_oversize_bytes() {
    let input = vec![b' '; MAX_REPORT_BYTES + 1];
    assert!(parse(&input).is_err());
}

#[cfg(unix)]
#[test]
fn report_load_rejects_symlink_even_when_target_is_valid() {
    let directory = TestDirectory::new();
    let path = directory.0.join("target.json");
    let link = directory.0.join("linked.json");
    fs::write(&path, bytes(&document())).expect("write synthetic report");
    std::os::unix::fs::symlink(&path, &link).expect("create test symlink");
    assert!(load(&link).is_err());
}

fn report_frame(
    app: &mut crate::report_app::ReportApp,
    ctx: &egui::Context,
    events: Vec<egui::Event>,
) -> egui::FullOutput {
    let mut output = ctx.run_ui(
        egui::RawInput {
            screen_rect: Some(egui::Rect::from_min_size(
                egui::Pos2::ZERO,
                egui::vec2(1600.0, 1200.0),
            )),
            events,
            ..Default::default()
        },
        |ui| app.draw(ui),
    );
    output.textures_delta.clear();
    output
}

fn report_texts(output: &egui::FullOutput) -> Vec<(String, egui::Rect)> {
    fn collect(shape: &egui::Shape, clip: egui::Rect, found: &mut Vec<(String, egui::Rect)>) {
        match shape {
            egui::Shape::Vec(shapes) => {
                for shape in shapes {
                    collect(shape, clip, found);
                }
            }
            egui::Shape::Text(text) => {
                let rect = text.galley.rect.translate(text.pos.to_vec2());
                if rect.intersects(clip) {
                    found.push((text.galley.text().to_owned(), rect.intersect(clip)));
                }
            }
            _ => {}
        }
    }
    let mut found = Vec::new();
    for shape in &output.shapes {
        collect(&shape.shape, shape.clip_rect, &mut found);
    }
    found
}

fn settled_report(app: &mut crate::report_app::ReportApp, ctx: &egui::Context) -> egui::FullOutput {
    let mut output = report_frame(app, ctx, Vec::new());
    for _ in 0..24 {
        let next = report_frame(app, ctx, Vec::new());
        if report_texts(&output) == report_texts(&next) {
            return next;
        }
        output = next;
    }
    panic!("report layout did not settle");
}

fn visible_report(output: &egui::FullOutput) -> String {
    report_texts(output)
        .into_iter()
        .map(|(text, _)| text)
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn report_empty_ui_explains_zero_rows_and_preserves_provenance_limits() {
    let mut expected = document();
    expected["observation_count"] = json!(0);
    expected["observations"] = json!([]);
    let report = parse(&bytes(&expected)).expect("empty report");
    let mut app = crate::report_app::ReportApp::new(report, None);
    let ctx = egui::Context::default();
    let text = visible_report(&settled_report(&mut app, &ctx));
    for required in [
        "No observations",
        "zero count",
        "UNSEALED",
        "no device authentication",
        "bytes checked",
        "reported, bytes unavailable",
    ] {
        assert!(
            text.contains(required),
            "missing empty-state or provenance explanation: {required}"
        );
    }
    assert_eq!(app.report.document, expected);
}

#[test]
fn report_click_selects_row_in_file_order_and_keeps_source_values_unchanged() {
    let mut expected = document();
    let mut second = expected["observations"][0].clone();
    second["observation_index"] = json!(1);
    second["t_ms"] = json!(-900);
    second["fields"][0]["value"] = json!("second-row-marker");
    second["fields"][0]["disposition"] = json!("present");
    expected["observations"]
        .as_array_mut()
        .expect("rows")
        .push(second);
    expected["observation_count"] = json!(2);
    let report = parse(&bytes(&expected)).expect("two rows");
    let original_hash = report.file_sha256.clone();
    let mut app = crate::report_app::ReportApp::new(report, None);
    let ctx = egui::Context::default();
    let initial = settled_report(&mut app, &ctx);
    assert_eq!(app.selected, 0);
    assert!(visible_report(&initial).contains("nonfinite_nan"));
    let point = report_texts(&initial)
        .iter()
        .find(|(text, _)| text.starts_with("#2  "))
        .expect("second row is visible")
        .1
        .center();
    report_frame(
        &mut app,
        &ctx,
        vec![
            egui::Event::PointerMoved(point),
            egui::Event::PointerButton {
                pos: point,
                button: egui::PointerButton::Primary,
                pressed: true,
                modifiers: egui::Modifiers::NONE,
            },
        ],
    );
    report_frame(
        &mut app,
        &ctx,
        vec![egui::Event::PointerButton {
            pos: point,
            button: egui::PointerButton::Primary,
            pressed: false,
            modifiers: egui::Modifiers::NONE,
        }],
    );
    let selected = visible_report(&settled_report(&mut app, &ctx));
    assert_eq!(app.selected, 1);
    assert!(selected.contains("second-row-marker"));
    assert!(selected.contains("t_ms: -900"));
    assert!(selected.contains("clock_basis: unknown"));
    assert!(selected.contains("wall_ms: UNKNOWN / null"));
    assert_eq!(app.report.document, expected);
    assert_eq!(app.report.file_sha256, original_hash);
}

#[test]
fn report_long_unicode_value_is_marked_as_shortened_without_mutating_evidence() {
    let mut expected = document();
    expected["observations"][0]["fields"][0]["value"] = json!("測".repeat(520));
    expected["observations"][0]["fields"][0]["disposition"] = json!("present");
    let report = parse(&bytes(&expected)).expect("long unicode value");
    let mut app = crate::report_app::ReportApp::new(report, None);
    let ctx = egui::Context::default();
    let text = visible_report(&settled_report(&mut app, &ctx));
    assert!(text.contains("display shortened; full value retained in the report"));
    assert!(!text.contains(&"測".repeat(520)));
    assert_eq!(app.report.document, expected);
}
