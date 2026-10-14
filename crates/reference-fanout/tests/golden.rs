//! Authored deterministic file-output and accounting regressions.

#![allow(
    clippy::doc_markdown,
    clippy::expect_used,
    clippy::too_many_lines,
    clippy::unwrap_used
)]

use musubi_northbound::fanout::SinkOutcome;
use musubi_reference_fanout::{
    DemoStep, SINK_COUNT, StepKind, build_steps, build_steps_with, concat_by_dialect, csvsink, run,
};
use musubi_reference_pipeline::export::DIGEST_PREFIX;
use musubi_reference_pipeline::{PipelineOut, run_pipeline};
use musubi_reference_scenario::{generate, pre_demo_default};

/// seed 42 の既定 scenario（contract synth と同じ種）→ pipeline → step 列。
fn seed42() -> (PipelineOut, Vec<DemoStep>) {
    let out = run_pipeline(&generate(&pre_demo_default(42)));
    let steps = build_steps(&out);
    (out, steps)
}

fn streams(out: &PipelineOut, steps: &[DemoStep]) -> std::collections::BTreeMap<String, Vec<u8>> {
    concat_by_dialect(&run(steps, out.t0_ms))
}

fn text(m: &std::collections::BTreeMap<String, Vec<u8>>, dialect: &str) -> String {
    String::from_utf8(m.get(dialect).cloned().unwrap_or_default()).expect("utf8")
}

#[test]
fn seed_42_fan_out_is_byte_identical_across_runs() {
    let (a_out, a_steps) = seed42();
    let (b_out, b_steps) = seed42();
    assert_eq!(a_steps.len(), b_steps.len());
    assert_eq!(streams(&a_out, &a_steps), streams(&b_out, &b_steps));
    // 単一 sealed 正本: fan-out は content_digest を書き直さない（step ごとに同じ digest）。
    for (x, y) in a_steps.iter().zip(&b_steps) {
        assert_eq!(x.envelope.content_digest, y.envelope.content_digest);
        assert_eq!(x.marker_id, y.marker_id);
    }
}

#[test]
fn every_step_reaches_every_sink_and_nothing_is_silently_dropped() {
    let (out, steps) = seed42();
    let report = run(&steps, out.t0_ms);
    assert_eq!(report.steps.len(), steps.len());
    for s in &report.steps {
        assert_eq!(
            s.report.outcomes.len(),
            SINK_COUNT,
            "step {} did not reach every sink",
            s.seq
        );
        // WITHHELD も MARK レジャに載る（silent drop と区別できる）。
        assert_eq!(
            s.report.mark_ledger.len(),
            SINK_COUNT,
            "step {} lost a MARK ledger entry",
            s.seq
        );
    }
    assert_eq!(report.published + report.withheld, steps.len() * SINK_COUNT);
    assert_eq!(report.failed, 0);
    // WITHHELD された raw は捨てず人間判断レーンへ（X-DEGRADE-2WAY）。
    assert_eq!(report.human_lane.pending_len(), report.withheld);
    assert!(report.withheld > 0, "the WITHHELD path must actually fire");
}

#[test]
fn legacy_position_only_sink_withholds_every_non_ok_envelope() {
    let (out, steps) = seed42();
    let report = run(&steps, out.t0_ms);
    let mut withheld_kinds = (0, 0, 0);
    for (s, step) in report.steps.iter().zip(&steps) {
        let outcome = s
            .report
            .outcome_for(csvsink::DIALECT_LEGACY)
            .expect("legacy sink present");
        let ok = step.mark().status == musubi_types::MarkStatus::Ok;
        match outcome {
            SinkOutcome::Withheld {
                mark, would_drop, ..
            } => {
                assert!(!ok, "an OK envelope must not be withheld ({})", s.marker_id);
                assert_eq!(mark.status, musubi_types::MarkStatus::Withheld);
                assert!(
                    mark.reason_code.contains(csvsink::DIALECT_LEGACY),
                    "the WITHHELD reason must name the sink: {}",
                    mark.reason_code
                );
                assert!(
                    would_drop.len() >= 3,
                    "the diff trail must list what would have vanished"
                );
                match step.kind {
                    StepKind::Absence => withheld_kinds.0 += 1,
                    StepKind::Cause => withheld_kinds.1 += 1,
                    // **A-1 回帰ガード**: source が止まった後の grid 点は「最後の fix」を
                    // 出し続けるので Degraded（stale）でなければならず、MARK を運べない sink では
                    // WITHHELD になる。以前ここは `panic!("fresh observations are OK")` で、
                    // 「死んだ機体の古い位置を MARK 無しで clean publish する」バグを固定していた。
                    StepKind::Observation => {
                        assert!(
                            step.mark().reason_code.contains("observation-stale"),
                            "only stale observations may be withheld: {}",
                            step.mark().reason_code
                        );
                        withheld_kinds.2 += 1;
                    }
                }
            }
            other => assert!(ok, "a non-OK envelope reached the sink anyway: {other:?}"),
        }
    }
    assert!(
        withheld_kinds.0 > 0 && withheld_kinds.1 > 0 && withheld_kinds.2 > 0,
        "absence, cause AND stale observations must all be withheld from a MARK-less sink: {withheld_kinds:?}"
    );
    // 位置だけの CSV に MARK 文字列が漏れていない（この sink の性質そのもの）。
    // 大文字（可視ラベル `DEGRADED`）と Debug 表記（`Degraded`）の **両方**を見る
    // ＝汎用 CSV は `{:?}` で書くので片方だけだと漏れを見逃す。
    let legacy = text(&streams(&out, &steps), csvsink::DIALECT_LEGACY);
    for leak in [
        "DEGRADED",
        "Degraded",
        // contract で汎用 CSV の語彙が snake_case になったので、小文字形も漏れの候補に入る。
        "degraded",
        "WITHHELD",
        "Withheld",
        "withheld",
        "absent-since",
        "observation-stale",
        "cause-candidate",
        "BASIS:",
    ] {
        assert!(
            !legacy.contains(leak),
            "MARK leaked into the legacy sink: {leak}"
        );
    }
    // 止まった source の古い位置が MARK 無しで流れていないこと＝同じ (marker, lat, lon) が
    // legacy CSV に重複しない（A-1 の症状そのもの: plane-01 の最後の fix が 4 行出ていた）。
    let mut seen: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for line in legacy.lines() {
        let cols: Vec<&str> = line.split(',').collect();
        assert!(cols.len() >= 4);
        let key = format!("{}|{}|{}", cols[0], cols[2], cols[3]);
        assert!(
            seen.insert(key.clone()),
            "the same position was published twice to the MARK-less sink: {key}"
        );
    }
}

#[test]
fn cot_puts_mark_absence_and_cause_in_remarks_without_changing_icon_or_affiliation() {
    let (out, steps) = seed42();
    let cot = text(&streams(&out, &steps), "cot");
    // 観測 marker（`ugv-01`）と absence marker が **同じ type**＝icon/affiliation を変えない
    // （contract §3-4(b)・F-8。MARK は remarks にだけ乗る）。
    assert!(cot.contains(r#"<event version="2.0" uid="ugv-01" type="a-u-G" how="m-g""#));
    assert!(
        cot.contains(r#"uid="ugv-01#absence:heartbeat.gcs@1788166859000" type="a-u-G" how="m-g""#)
    );
    assert!(cot.contains(r#"uid="plane-01" type="a-u-A""#));
    assert!(
        !cot.contains("a-f-") && !cot.contains("a-h-"),
        "Musubi never asserts affiliation (friend/hostile)"
    );
    // 時刻: observed_at(µs)→ms の単位契約が壊れると 1970 になる（contract）。ISO を丸ごと固定する。
    assert!(cot.contains(r#"time="2026-08-31T09:00:00.000Z" start="2026-08-31T09:00:00.000Z" stale="2026-08-31T09:00:15.000Z""#));
    // 位置: family ごとに実座標（degE7／十進度／`"lat lon"` 文字列の取り違えを golden で止める）。
    assert!(
        cot.contains(r#"<point lat="35.6762" lon="139.6502999""#),
        "ugv"
    );
    assert!(cot.contains(r#"<point lat="35.7" lon="139.7""#), "plane");
    assert!(cot.contains(r#"<point lat="35.65" lon="139.6""#), "fpv");
    // absence: remarks の MARK 欄に status・reason token・clock_basis・不在の時刻が全部残る。
    // absence marker の `time` は `effective_time_ms`＝`observed_at`＝**absent_since**（09:00:59）で
    // あって step の時刻（＝気づけた時刻 expected_by・09:01:02＝CSV の `at_utc`）ではない。
    // sink 間で「時刻」が指すものが違う点を固定する（docs/offline-components.md）。
    assert!(cot.contains(concat!(
        r#"uid="ugv-01#absence:heartbeat.gcs@1788166859000" type="a-u-G" how="m-g" "#,
        r#"time="2026-08-31T09:00:59.000Z" start="2026-08-31T09:00:59.000Z" stale="2026-08-31T09:01:14.000Z""#
    )));
    let absence = cot
        .lines()
        .find(|l| l.contains("absent-since:") && l.contains("heartbeat"))
        .expect("an absence remark");
    for token in [
        "<remarks>MARK:DEGRADED:absence-expected-cadence",
        "clock-basis:host_received",
        "absent-channel:heartbeat",
        "absent-since:2026-08-31T09:00:59.000Z",
        "expected-by:2026-08-31T09:01:02.000Z",
        "expectation:heartbeat_1hz@",
        "last-good:",
        ";CONF:0.00;PROV:",
        ";BASIS:adapter-assigned</remarks>",
    ] {
        assert!(absence.contains(token), "missing {token} in {absence}");
    }
    // cause: 候補であって verdict ではないことが wire に残る。
    let cause = cot
        .lines()
        .find(|l| l.contains("cause:telemetry_link_loss"))
        .expect("a cause remark");
    for token in [
        "MARK:DEGRADED:cause-candidate",
        "trigger-channel:heartbeat",
        "cause:telemetry_link_loss+alt0",
        "candidate-not-verdict",
        "catalog:",
    ] {
        assert!(cause.contains(token), "missing {token} in {cause}");
    }
    // reason token に `;` を混ぜない（contract: remarks のトップレベル区切りと衝突する）。
    for line in cot.lines().filter(|l| l.contains("<remarks>")) {
        let inner = line.split("<remarks>").nth(1).expect("remarks").trim_end();
        assert_eq!(
            inner.matches(';').count(),
            3,
            "remarks must stay a 4-field MARK;CONF;PROV;BASIS record: {inner}"
        );
    }
}

#[test]
fn nvg_puts_mark_in_label_and_modifiers_with_unknown_affiliation() {
    let (out, steps) = seed42();
    let nvg = text(&streams(&out, &steps), "nvg");
    assert!(
        nvg.contains(r#"symbol="app6b:SUGP------*****""#),
        "ugv = unknown+ground"
    );
    assert!(
        nvg.contains(r#"symbol="app6b:SUAP------*****""#),
        "air = unknown+air"
    );
    assert!(!nvg.contains("SFG") && !nvg.contains("SFA"), "never friend");
    let absence = nvg
        .lines()
        .find(|l| l.contains("absent-since:") && l.contains("heartbeat"))
        .expect("an absence point");
    // NVG は x=経度 / y=緯度（CoT と逆順）。取り違えを golden で止める。
    assert!(absence.contains(r#"x="139.6502999""#) && absence.contains(r#"y="35.676731""#));
    assert!(
        absence.contains(r#"label="ugv-01#absence:heartbeat.gcs@1788166859000 [DEGRADED:absence-expected-cadence"#),
        "MARK は label にも出る（人が読む欄）: {absence}"
    );
    assert!(
        absence.contains(r#"modifiers="MARK:DEGRADED;REASON:absence-expected-cadence"#)
            && absence.contains("absent-since:2026-08-31T09:00:59.000Z")
            && absence.contains(";CONF:0.00;BASIS:adapter-assigned\""),
        "MARK は modifiers にも出る（機械が読む欄）: {absence}"
    );
    assert!(nvg.contains(r#"<TimeStamp when="2026-08-31T09:00:59.000Z"/>"#));
}

#[test]
fn generic_csv_keeps_mark_in_its_own_columns_and_never_states_a_verdict() {
    let (out, steps) = seed42();
    let csv = text(&streams(&out, &steps), csvsink::DIALECT_CSV);
    // **contract 回帰ガード**: 以前ここは `csv.contains("Degraded")`（`{:?}` 由来 CamelCase）で旧表記を
    // 凍結していた。列を header 名で引き、値は snake_case 語彙であることを見る。
    let t = events_table(&csv);
    assert!(t.column("mark_status").contains(&"degraded"));
    assert!(
        t.column("mark_reason_code")
            .iter()
            .any(|c| c.contains("absent-since:"))
    );
    assert!(csv.contains("candidate, not a verdict"));
    // contract §2.1: 順位・推奨・宛先・desired state は **field 名としても存在しない**。
    for banned in [
        "recommended_action",
        "priority",
        "route",
        "destination",
        "desired_state",
        "authority_grant",
    ] {
        assert!(!csvsink::CSV_HEADER.contains(banned) && !csv.contains(banned));
    }
    // `concat_by_dialect` の出力に header 行は無い（header は bin が足す）ので全行がデータ行。
    // step 数と行数が一致＝1 step 1 行で、途中で黙って落ちた行が無い。
    assert_eq!(csv.lines().count(), steps.len());
    assert!(csv.lines().all(|l| !l.trim().is_empty()));
    // cause の 3 状態が列にそのまま残る（unknown を落とさない）。
    assert!(csv.contains("consistent with ") || csv.contains("ambiguous between "));
}

#[test]
fn near_now_shift_moves_every_time_together_and_keeps_the_structure() {
    let out = run_pipeline(&generate(&pre_demo_default(42)));
    let base = build_steps_with(&out, 0);
    let shift = 7 * 24 * 3_600_000; // 1 週間ぶん
    let moved = build_steps_with(&out, shift);
    assert_eq!(base.len(), moved.len());
    for (a, b) in base.iter().zip(&moved) {
        assert_eq!(a.kind, b.kind);
        assert_eq!(b.at_wall_ms - a.at_wall_ms, shift);
        assert_eq!(b.observed_at_wall_ms - a.observed_at_wall_ms, shift);
        // MARK の中の ISO 文言も同じだけ動く（バイト列を後から置換しない＝時刻が食い違わない）。
        if a.kind == StepKind::Absence {
            assert!(a.mark().reason_code.contains("absent-since:2026-08-31T"));
            assert!(b.mark().reason_code.contains("absent-since:2026-09-07T"));
        }
    }
    let moved_cot = text(&streams(&out, &moved), "cot");
    assert!(moved_cot.contains(r#"time="2026-09-07T09:00:00.000Z""#));
}

// ------------------------------------------------------------------ contract（contract C-2）の回帰ガード
//
// contract が `crates/reference-pipeline/tests/export.rs` に入れたのと同じ 3 property を、fan-out の
// 汎用 CSV sink にも置く。helper は **test 側の重複**（実装側は共有＝`export::mark_label`／
// `export::DIGEST_PREFIX` を import している）。zero-dep 方針のため csv/regex crate は足さない。

/// RFC 4180 最小 parser（quoted 内の `,`／`""` escape を扱う）。
fn parse_csv(s: &str) -> Vec<Vec<String>> {
    let (mut rows, mut row, mut cell, mut quoted) = (Vec::new(), Vec::new(), String::new(), false);
    let mut it = s.chars().peekable();
    while let Some(ch) = it.next() {
        if quoted {
            if ch == '"' {
                if it.peek() == Some(&'"') {
                    it.next();
                    cell.push('"');
                } else {
                    quoted = false;
                }
            } else {
                cell.push(ch);
            }
        } else {
            match ch {
                '"' => quoted = true,
                ',' => row.push(std::mem::take(&mut cell)),
                '\n' => {
                    row.push(std::mem::take(&mut cell));
                    rows.push(std::mem::take(&mut row));
                }
                _ => cell.push(ch),
            }
        }
    }
    assert!(
        !quoted && cell.is_empty() && row.is_empty(),
        "CSV must end with a complete row and a trailing LF"
    );
    rows
}

/// header 名で列を引ける表。
struct Table {
    header: Vec<String>,
    rows: Vec<Vec<String>>,
}

impl Table {
    fn at(&self, row: usize, name: &str) -> &str {
        let i = self
            .header
            .iter()
            .position(|h| h == name)
            .unwrap_or_else(|| panic!("no column named {name}: {:?}", self.header));
        &self.rows[row][i]
    }

    fn column(&self, name: &str) -> Vec<&str> {
        (0..self.rows.len()).map(|r| self.at(r, name)).collect()
    }
}

/// `concat_by_dialect` は data 行だけを返す（header は bin が足す）ので、検査のために header を被せる。
fn events_table(body: &str) -> Table {
    let mut all = parse_csv(&format!("{}\n{body}", csvsink::CSV_HEADER));
    let header = all.remove(0);
    for (i, r) in all.iter().enumerate() {
        assert_eq!(
            r.len(),
            header.len(),
            "row {i} has a different column count"
        );
    }
    assert!(!all.is_empty(), "no data rows");
    Table { header, rows: all }
}

/// 表計算が**日付**に変換してしまう形（`2/2`・`1-2`・`1/2/26`）。cell 全体で判定する
/// （部分一致にすると ISO 時刻や版番号に誤検知する＝contract と同じ理由）。
fn looks_like_a_date(cell: &str) -> bool {
    let parts: Vec<&str> = cell.split(['/', '-']).collect();
    (parts.len() == 2 || parts.len() == 3)
        && parts
            .iter()
            .all(|p| !p.is_empty() && p.len() <= 4 && p.bytes().all(|b| b.is_ascii_digit()))
}

/// 表計算が**数値**として読み、桁や表記を書き換えてしまう形（指数形・先頭 0・16 桁以上）。
fn looks_numeric_to_a_spreadsheet(cell: &str) -> bool {
    let b = cell.as_bytes();
    let digits = |x: &[u8]| !x.is_empty() && x.iter().all(u8::is_ascii_digit);
    if let Some(i) = cell.find(['e', 'E'])
        && digits(&b[..i])
    {
        let rest = &b[i + 1..];
        let rest = match rest.first() {
            Some(b'+' | b'-') => &rest[1..],
            _ => rest,
        };
        if digits(rest) {
            return true;
        }
    }
    digits(b) && (b.len() >= 16 || (b.len() > 1 && b[0] == b'0'))
}

#[test]
fn every_digest_cell_in_the_generic_csv_carries_the_sha256_prefix() {
    let (out, steps) = seed42();
    let t = events_table(&text(&streams(&out, &steps), csvsink::DIALECT_CSV));
    let mut seen = 0;
    for cell in t.column("evidence_digest8") {
        // `-` は「この step に content_digest が無い」ことを示す sentinel であって digest ではない
        // （contract の `never-observed` と同じ扱い）。それ以外は必ず接頭辞つきの hex。
        if cell == "-" {
            continue;
        }
        seen += 1;
        let hex = cell
            .strip_prefix(DIGEST_PREFIX)
            .unwrap_or_else(|| panic!("digest cell without the {DIGEST_PREFIX} prefix: {cell}"));
        assert!(
            hex.len() == 8
                && hex
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()),
            "digest cell is not 8 lowercase hex chars: {cell}"
        );
        assert!(
            !cell.as_bytes()[0].is_ascii_digit(),
            "a digest cell must not start with a digit: {cell}"
        );
    }
    assert!(seen > 0, "no digest cells were checked");
}

#[test]
fn no_generic_csv_cell_is_rewritten_by_a_spreadsheet() {
    let (out, steps) = seed42();
    let t = events_table(&text(&streams(&out, &steps), csvsink::DIALECT_CSV));
    let mut checked = 0;
    for (r, row) in t.rows.iter().enumerate() {
        for (c, cell) in row.iter().enumerate() {
            // 多値の cell は `|` で分けてから見る（contract と同じ規則）。
            for part in cell.split('|') {
                checked += 1;
                assert!(
                    !looks_like_a_date(part) && !looks_numeric_to_a_spreadsheet(part),
                    "row {r} column {} would be rewritten by a spreadsheet: {part}",
                    t.header[c]
                );
            }
        }
    }
    assert!(checked > 100, "too few cells checked ({checked})");
    // ガード自身に teeth があること（contract と同じ形の自己検査）。8 文字 hex の危険な形は
    // (a) 指数形（`1234e567` → 1.234E+570）と (b) 先頭 0 の全数字（`01234567` → 1234567）で、
    // 接頭辞が付くとどちらも数字で始まらなくなるので表計算は text として扱う。
    for bad in [
        "2498108893182e42",
        "1234e567",
        "01234567",
        "007",
        "1234567890123456",
    ] {
        assert!(looks_numeric_to_a_spreadsheet(bad), "{bad}");
    }
    for ok in [
        "sha256:2498108893182e42",
        "sha256:01234567",
        "c3fba10d",      // 英字を含む hex は数値に読まれない
        "1788166862000", // epoch ms（13 桁・値は保たれる）
    ] {
        assert!(!looks_numeric_to_a_spreadsheet(ok), "{ok}");
    }
    assert!(looks_like_a_date("2/2"));
    assert!(!looks_like_a_date("2026-08-31T09:01:02.000Z"));
}

#[test]
fn generic_csv_mark_status_uses_the_same_snake_case_vocabulary_as_the_p04_export() {
    use musubi_reference_pipeline::export::mark_label;
    use musubi_types::MarkStatus;

    // 語彙の正本は contract の書き出しの表（`export::mark_label`）。ここで綴りを再掲するのは、
    // 表が黙って変わったら（＝2 つの CSV の語彙がずれたら）落とすため。
    let vocab = [
        (MarkStatus::Ok, "ok"),
        (MarkStatus::Degraded, "degraded"),
        (MarkStatus::Invalid, "invalid"),
        (MarkStatus::OrderUnknown, "order_unknown"),
        (MarkStatus::Withheld, "withheld"),
        (MarkStatus::StaleRevocation, "stale_revocation"),
        (MarkStatus::SelfAssertedTime, "self_asserted_time"),
        (MarkStatus::BufferSaturated, "buffer_saturated"),
    ];
    for (variant, label) in vocab {
        assert_eq!(mark_label(variant), label, "shared label table drifted");
    }

    let (out, steps) = seed42();
    let t = events_table(&text(&streams(&out, &steps), csvsink::DIALECT_CSV));
    let allowed: Vec<&str> = vocab.iter().map(|(_, l)| *l).collect();
    for cell in t.column("mark_status") {
        assert!(
            allowed.contains(&cell),
            "mark_status left the shared snake_case vocabulary: {cell}"
        );
    }
    // `{:?}` 由来の CamelCase が 1 つも残っていない（file 全体で見る＝列名にも値にも出ない）。
    let csv = text(&streams(&out, &steps), csvsink::DIALECT_CSV);
    for camel in [
        "Ok",
        "Degraded",
        "Invalid",
        "OrderUnknown",
        "Withheld",
        "StaleRevocation",
        "SelfAssertedTime",
        "BufferSaturated",
    ] {
        assert!(
            !csv.contains(camel),
            "a CamelCase MarkStatus is still written into the generic CSV: {camel}"
        );
    }
}
