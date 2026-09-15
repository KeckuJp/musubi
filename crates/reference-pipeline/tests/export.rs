#![allow(clippy::doc_markdown, clippy::expect_used, clippy::unwrap_used)]

use musubi_reference_pipeline::export::{
    CANDIDATE_SELECTION_RULE, DIGEST_PREFIX, EVIDENCE_DIGESTS_HEAD, EXPORT_SCHEMA_VERSION,
    NOTE_CANDIDATE, QUESTION_7, absences_csv, answer_text, claims_csv, export_all, stand_in_lines,
};
use musubi_reference_pipeline::{Knowledge, PipelineOut, metrics, run_pipeline};
use musubi_reference_scenario::{
    ChannelId, FailureKind, Family, SourceRole, Timeline, generate, pre_demo_default,
};
use musubi_reference_types::{CauseOutcome, DigestRef};

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

struct Table {
    header: Vec<String>,
    rows: Vec<Vec<String>>,
}

impl Table {
    fn of(text: &str) -> Self {
        let mut all = parse_csv(text);
        assert!(!all.is_empty(), "no header line");
        let header = all.remove(0);
        for (i, r) in all.iter().enumerate() {
            assert_eq!(
                r.len(),
                header.len(),
                "row {i} has a different column count"
            );
        }
        Self { header, rows: all }
    }

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

fn looks_like_a_date(cell: &str) -> bool {
    let parts: Vec<&str> = cell.split(['/', '-']).collect();
    (parts.len() == 2 || parts.len() == 3)
        && parts
            .iter()
            .all(|p| !p.is_empty() && p.len() <= 4 && p.bytes().all(|b| b.is_ascii_digit()))
}

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

const DIGEST_COLUMNS: [&str; 5] = [
    "last_good_digest",
    "next_good_digest",
    "absence_digest",
    "absence_digests",
    "evidence_digests_head",
];

const DIGEST_SENTINELS: [&str; 2] = ["never-observed", "open-end"];

const WALL_CELLS: [&str; 4] = [
    "absent_since_utc",
    "absent_since_wall_ms",
    "expected_by_wall_ms",
    "wall_bound_ms",
];

fn is_sql_identifier(name: &str) -> bool {
    let mut b = name.bytes();
    b.next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == b'_')
        && name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
}

fn reserialize(t: &Table) -> String {
    let quote = |s: &str| {
        if s.contains([',', '"', '\n']) {
            format!("\"{}\"", s.replace('"', "\"\""))
        } else {
            s.to_string()
        }
    };
    std::iter::once(&t.header)
        .chain(t.rows.iter())
        .map(|r| {
            let mut line = r.iter().map(|c| quote(c)).collect::<Vec<_>>().join(",");
            line.push('\n');
            line
        })
        .collect()
}

fn abstention_variant() -> (Timeline, PipelineOut) {
    let mut s = pre_demo_default(7);
    s.injections.clear();
    s.assets[0].suppressed_channels = vec![ChannelId::LinkStats];
    s.assets[0].sources = vec![SourceRole::Gcs];
    s.assets[2].sources = vec![SourceRole::Video];
    s.inject(FailureKind::FcFailure, 60_000, 60_000, Family::Ugv);
    s.inject(FailureKind::CameraStop, 60_000, 30_000, Family::Fpv);
    let tl = generate(&s);
    let out = run_pipeline(&tl);
    (tl, out)
}

#[test]
fn csv_exports_have_one_row_per_absence_and_claim_and_keep_unknown() {
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    let a = Table::of(&absences_csv(&out));
    let c = Table::of(&claims_csv(&out, &k));
    assert_eq!(a.rows.len(), out.noticed.len() + out.unmapped.len());
    assert_eq!(c.rows.len(), out.claims.len());
    let n_unknown = out
        .claims
        .iter()
        .filter(|x| matches!(x.claim.outcome, CauseOutcome::Unknown { .. }))
        .count();
    let n_amb = out
        .claims
        .iter()
        .filter(|x| matches!(x.claim.outcome, CauseOutcome::Ambiguous { .. }))
        .count();
    assert!(n_unknown + n_amb > 0, "variant should produce abstention");
    let outcomes = c.column("outcome");
    assert_eq!(
        outcomes.iter().filter(|v| **v == "unknown").count(),
        n_unknown
    );
    assert_eq!(
        outcomes.iter().filter(|v| **v == "ambiguous").count(),
        n_amb
    );
    for r in 0..c.rows.len() {
        if c.at(r, "outcome") == "ambiguous" {
            assert_eq!(c.at(r, "candidate_signature"), "");
            assert_eq!(c.at(r, "consistent_with"), "");
            assert_eq!(
                c.at(r, "logic_confidence"),
                "per-candidate (see alternatives)"
            );
        }
        assert_eq!(c.at(r, "note"), NOTE_CANDIDATE);
        assert_eq!(
            c.at(r, "candidate_selection_rule"),
            CANDIDATE_SELECTION_RULE
        );
        assert_eq!(c.at(r, "export_schema_version"), EXPORT_SCHEMA_VERSION);
    }
    for r in 0..a.rows.len() {
        assert_eq!(a.at(r, "export_schema_version"), EXPORT_SCHEMA_VERSION);
    }
    assert!(!absences_csv(&out).contains('%'));
    assert!(!claims_csv(&out, &k).contains('%'));
}

#[test]
fn every_column_name_is_a_sql_identifier_so_sqlite_and_jq_do_not_fail_silently() {
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    for t in [
        Table::of(&absences_csv(&out)),
        Table::of(&claims_csv(&out, &k)),
    ] {
        for h in &t.header {
            assert!(
                is_sql_identifier(h),
                "column name is not an identifier: {h}"
            );
        }
    }
}

#[test]
fn match_and_basis_mix_columns_are_integers_never_a_spreadsheet_date() {
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    let c = Table::of(&claims_csv(&out, &k));
    assert!(
        !c.header
            .iter()
            .any(|h| h == "match_required" || h.starts_with("basis_mix("))
    );
    for name in [
        "match_cues_matched",
        "match_cues_required",
        "match_contradicting",
    ] {
        for (r, v) in c.column(name).iter().enumerate() {
            let consistent = c.at(r, "outcome") == "consistent";
            assert_eq!(!v.is_empty(), consistent, "{name} row {r} = {v:?}");
            assert!(v.is_empty() || v.parse::<u32>().is_ok(), "{name} = {v:?}");
        }
    }
    for name in [
        "basis_mix_adapter",
        "basis_mix_measured",
        "basis_mix_unspecified",
    ] {
        for v in c.column(name) {
            assert!(v.parse::<u32>().is_ok(), "{name} = {v:?}");
        }
    }
    for name in [
        "absence_refs",
        "evidence_count",
        "window_start_ms",
        "window_end_ms",
    ] {
        for v in c.column(name) {
            assert!(v.parse::<i64>().is_ok(), "{name} = {v:?}");
        }
    }
}

#[test]
fn absence_digest_joins_absences_to_cause_claims() {
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    let a = Table::of(&absences_csv(&out));
    let c = Table::of(&claims_csv(&out, &k));
    let left: std::collections::BTreeSet<&str> = a.column("absence_digest").into_iter().collect();
    assert_eq!(left.len(), a.rows.len(), "absence_digest must be unique");
    let mut joined = 0usize;
    for r in 0..c.rows.len() {
        let refs = c.at(r, "absence_digests");
        let parts: Vec<&str> = if refs.is_empty() {
            Vec::new()
        } else {
            refs.split('|').collect()
        };
        assert_eq!(parts.len().to_string(), c.at(r, "absence_refs"));
        for d in &parts {
            assert!(d.starts_with(DIGEST_PREFIX), "{d}");
            assert_eq!(
                d.len(),
                DIGEST_PREFIX.len() + 16,
                "digest is sha256: + the first 8 bytes as hex: {d}"
            );
            assert!(
                left.contains(d),
                "claim row {r} references an unknown absence {d}"
            );
            joined += 1;
        }
    }
    assert!(
        joined > 0,
        "the variant must produce at least one joinable row"
    );
}

#[test]
fn full_references_preserve_colliding_prefixes_all_evidence_and_absence_join() {
    use musubi_reference_pipeline::export::digest_full_cell;
    let (_, mut out) = abstention_variant();
    let k = Knowledge::repo_public();
    let evidence: Vec<DigestRef> = (0u8..10)
        .map(|suffix| {
            let mut bytes = [7; 32];
            bytes[31] = suffix;
            DigestRef(bytes)
        })
        .collect();
    out.claims[0].claim.evidence = evidence.clone();
    let c = Table::of(&claims_csv(&out, &k));
    let a = Table::of(&absences_csv(&out));
    let full: Vec<&str> = c.at(0, "evidence_digests_full").split('|').collect();
    assert_eq!(full.len(), 10);
    assert_eq!(c.at(0, "evidence_count"), "10");
    assert_eq!(
        c.at(0, "evidence_digests_head").split('|').count(),
        EVIDENCE_DIGESTS_HEAD
    );
    for (actual, digest) in full.iter().zip(&evidence) {
        assert_eq!(*actual, digest_full_cell(digest));
        assert_eq!(actual.len(), DIGEST_PREFIX.len() + 64);
        assert!(!looks_numeric_to_a_spreadsheet(actual));
    }
    assert_eq!(
        full.iter().collect::<std::collections::BTreeSet<_>>().len(),
        10
    );
    let left: std::collections::BTreeSet<&str> =
        a.column("absence_digest_full").into_iter().collect();
    let mut joined = 0;
    for r in 0..c.rows.len() {
        let refs: Vec<&str> = c
            .at(r, "absence_digests_full")
            .split('|')
            .filter(|v| !v.is_empty())
            .collect();
        assert_eq!(refs.len().to_string(), c.at(r, "absence_refs"));
        for value in refs {
            assert_eq!(value.len(), DIGEST_PREFIX.len() + 64);
            assert!(left.contains(value));
            joined += 1;
        }
    }
    assert!(joined > 0);
    for (index, noticed) in out.noticed.iter().enumerate() {
        assert_eq!(
            a.at(index, "last_good_digest_full"),
            noticed
                .negative
                .last_good
                .as_ref()
                .map_or("never-observed".into(), digest_full_cell)
        );
    }
    out.noticed[0].negative.last_good = None;
    let without_last_good = Table::of(&absences_csv(&out));
    assert_eq!(
        without_last_good.at(0, "last_good_digest_full"),
        "never-observed"
    );
}

fn check_gap_end_rows(out: &PipelineOut, a: &Table) -> (usize, usize, usize) {
    use musubi_reference_pipeline::export::digest_full_cell;
    use musubi_reference_readers::absence::GapEnd;
    let (mut closed, mut open, mut unresolved) = (0usize, 0usize, 0usize);
    assert_eq!(a.rows.len(), out.noticed.len() + out.unmapped.len());
    for (index, n) in out.noticed.iter().enumerate() {
        let end = a.at(index, "gap_end");
        assert_eq!(end, n.bounds.end.as_label());
        let Some(digest) = n.bounds.next_good else {
            assert_eq!(n.bounds.end, GapEnd::OpenEnd);
            assert_eq!(a.at(index, "next_good_digest"), "open-end");
            assert_eq!(a.at(index, "next_good_digest_full"), "open-end");
            assert_eq!(a.at(index, "next_good_native_ms"), "");
            assert_eq!(a.at(index, "next_good_clock_basis"), "");
            open += 1;
            continue;
        };
        let source = &out
            .assets
            .iter()
            .find(|x| x.asset_id == n.asset_id)
            .expect("asset")
            .sources[n.source_index];
        assert!(
            source
                .obs
                .iter()
                .any(|o| o.digest == digest && o.channel == n.negative.subject.channel),
            "next_good must reference an observation of the same channel"
        );
        let full = a.at(index, "next_good_digest_full");
        assert_eq!(full, digest_full_cell(&digest));
        assert_eq!(full.len(), DIGEST_PREFIX.len() + 64);
        assert_ne!(
            full,
            a.at(index, "next_good_digest"),
            "full is not the head"
        );
        assert_eq!(
            a.at(index, "next_good_clock_basis"),
            n.bounds
                .next_good_clock_basis
                .expect("a referenced observation carries its basis")
                .as_label()
        );
        if end == "next_observation" {
            let native: i64 = a
                .at(index, "next_good_native_ms")
                .parse()
                .expect("native ms");
            assert!(native > n.negative.absent_since_ms);
            assert_eq!(
                a.at(index, "next_good_clock_basis"),
                n.negative.clock_basis.as_label(),
                "a closed end is on the gap's own axis"
            );
            closed += 1;
        } else {
            assert_eq!(end, "next_observation_clock_unresolved");
            assert_eq!(a.at(index, "next_good_native_ms"), "");
            unresolved += 1;
        }
    }
    (closed, open, unresolved)
}

#[test]
fn every_gap_row_locates_its_end_and_joins_the_next_observation_by_full_digest() {
    use musubi_reference_pipeline::export::digest_full_cell;
    use musubi_reference_readers::absence::GapEnd;
    let mut out = run_pipeline(&generate(&pre_demo_default(42)));
    let a = Table::of(&absences_csv(&out));
    let (closed, open, unresolved) = check_gap_end_rows(&out, &a);
    let (_, other) = abstention_variant();
    let second = Table::of(&absences_csv(&other));
    let (c2, o2, u2) = check_gap_end_rows(&other, &second);
    assert!(
        closed + c2 > 0 && open + o2 > 0 && unresolved + u2 > 0,
        "the two seeds must show a closed, an open and a clock-unresolved end (closed {}, open {}, unresolved {})",
        closed + c2,
        open + o2,
        unresolved + u2
    );
    let repeated = DigestRef([3; 32]);
    for row in out.noticed.iter_mut().take(2) {
        row.bounds.end = GapEnd::NextObservation;
        row.bounds.next_good = Some(repeated);
        row.bounds.next_good_ms = Some(4_242);
    }
    let t = Table::of(&absences_csv(&out));
    assert_eq!(t.rows.len(), out.noticed.len());
    assert_eq!(
        t.at(0, "next_good_digest_full"),
        t.at(1, "next_good_digest_full")
    );
    assert_eq!(
        t.at(0, "next_good_digest_full"),
        digest_full_cell(&repeated)
    );
    assert_eq!(t.at(0, "next_good_native_ms"), "4242");
    let mut bytes = [5u8; 32];
    let first = DigestRef(bytes);
    bytes[31] = 9;
    let second_digest = DigestRef(bytes);
    out.noticed[0].bounds.next_good = Some(first);
    out.noticed[1].bounds.next_good = Some(second_digest);
    let t = Table::of(&absences_csv(&out));
    assert_eq!(t.at(0, "next_good_digest"), t.at(1, "next_good_digest"));
    assert_ne!(
        t.at(0, "next_good_digest_full"),
        t.at(1, "next_good_digest_full")
    );
    assert_eq!(t.at(0, "next_good_digest_full"), digest_full_cell(&first));
    assert_eq!(
        t.at(1, "next_good_digest_full"),
        digest_full_cell(&second_digest)
    );
    assert_eq!(
        t.column("absence_digest_full"),
        a.column("absence_digest_full")
    );
}

#[test]
fn unmappable_source_keeps_its_rows_with_empty_wall_cells_and_a_reason() {
    use musubi_reference_pipeline::export::WALL_UNMAPPED_UNKNOWN_CLOCK;
    use musubi_reference_pipeline::{ObservationWindow, WALL_MAPPED, analyze};
    use musubi_reference_types::ClockBasis;
    let base = run_pipeline(&generate(&pre_demo_default(42)));
    let detected = |out: &PipelineOut| -> usize {
        out.assets
            .iter()
            .flat_map(|a| a.sources.iter())
            .map(|s| s.negatives.len())
            .sum()
    };
    assert!(base.unmapped.is_empty());
    assert_eq!(base.noticed.len(), detected(&base));
    let mapped_before = Table::of(&absences_csv(&base));

    let mut k = Knowledge::repo_public();
    for p in &mut k.profiles {
        if p.profile_id == "edgetx_handset_csv" {
            p.default_clock_basis = ClockBasis::BootRelative;
        }
    }
    let out = analyze(
        base.files.clone(),
        &k,
        ObservationWindow::Explicit {
            t0_ms: base.t0_ms,
            end_ms: base.end_ms,
        },
    )
    .expect("analyze");
    assert!(
        !out.unmapped.is_empty(),
        "the declared boot axis must strand at least one absence"
    );
    assert_eq!(out.noticed.len() + out.unmapped.len(), detected(&out));
    assert!(
        out.noticed.len() < base.noticed.len(),
        "some rows moved off the wall path"
    );

    let a = Table::of(&absences_csv(&out));
    assert_eq!(a.rows.len(), out.noticed.len() + out.unmapped.len());
    let digests: Vec<&str> = a.column("absence_digest_full");
    assert_eq!(
        digests
            .iter()
            .collect::<std::collections::BTreeSet<_>>()
            .len(),
        digests.len(),
        "every detected absence appears exactly once"
    );
    let mut unknown_noticed = 0;
    for (index, n) in out.noticed.iter().enumerate() {
        if n.negative.clock_basis == ClockBasis::Unknown {
            unknown_noticed += 1;
            assert_eq!(a.at(index, "wall_mapping"), WALL_UNMAPPED_UNKNOWN_CLOCK);
            for empty in WALL_CELLS {
                assert_eq!(
                    a.at(index, empty),
                    "",
                    "{empty} must stay empty for an unknown clock"
                );
            }
            continue;
        }
        assert_eq!(a.at(index, "wall_mapping"), WALL_MAPPED);
        assert_eq!(
            a.at(index, "absent_since_wall_ms"),
            n.since_wall_ms.to_string()
        );
        assert_eq!(a.at(index, "expected_by_wall_ms"), n.by_wall_ms.to_string());
        assert_eq!(a.at(index, "wall_bound_ms"), n.bound_ms.to_string());
        assert!(!a.at(index, "absent_since_utc").is_empty());
    }
    assert!(
        unknown_noticed > 0,
        "this corpus must still contain an unknown-clock absence on the legacy noticed path"
    );
    for (offset, u) in out.unmapped.iter().enumerate() {
        let index = out.noticed.len() + offset;
        let reason = if u.negative.clock_basis == ClockBasis::Unknown {
            WALL_UNMAPPED_UNKNOWN_CLOCK
        } else {
            "unmapped_no_time_offset"
        };
        assert_eq!(a.at(index, "wall_mapping"), reason);
        for empty in WALL_CELLS {
            assert_eq!(a.at(index, empty), "", "{empty} must stay empty, not 0");
        }
        assert_eq!(
            a.at(index, "absent_since_native_ms"),
            u.negative.absent_since_ms.to_string()
        );
        assert_eq!(
            a.at(index, "expected_by_native_ms"),
            u.negative.expected_by_ms.to_string()
        );
        assert_eq!(
            a.at(index, "clock_basis"),
            u.negative.clock_basis.as_label()
        );
        let source = &out
            .assets
            .iter()
            .find(|x| x.asset_id == u.asset_id)
            .expect("asset")
            .sources[u.source_index];
        assert_eq!(a.at(index, "asset_id"), u.asset_id);
        assert_eq!(a.at(index, "file"), source.file_name);
        assert_eq!(a.at(index, "profile_id"), source.profile.profile_id);
        assert_eq!(
            a.at(index, "source_role"),
            source.profile.source_role.as_str()
        );
        assert_eq!(a.at(index, "channel"), u.negative.subject.channel.as_str());
        assert_eq!(a.at(index, "expectation_id"), u.negative.expectation_id);
        assert!(
            a.at(index, "absence_digest_full")
                .starts_with(DIGEST_PREFIX)
        );
        assert_eq!(a.at(index, "gap_end"), u.bounds.end.as_label());
        if u.bounds.end.as_label() != "next_observation" {
            assert_eq!(a.at(index, "next_good_native_ms"), "");
        }
        if u.negative.clock_basis == ClockBasis::Unknown {
            assert_ne!(a.at(index, "gap_end"), "next_observation");
        }
    }
    assert!(
        out.unmapped.iter().any(|u| {
            u.negative.clock_basis == ClockBasis::Unknown
                && u.bounds.end.as_label() == "next_observation_clock_unresolved"
        }),
        "an all-unknown-clock source must keep both its basis and its end explicitly uncertain"
    );
    let shared: Vec<&str> = mapped_before
        .header
        .iter()
        .map(std::string::String::as_str)
        .collect();
    for (index, n) in base.noticed.iter().enumerate() {
        let same = out
            .noticed
            .iter()
            .position(|m| m.negative == n.negative && m.asset_id == n.asset_id);
        if let Some(target) = same {
            for name in &shared {
                assert_eq!(
                    a.at(target, name),
                    mapped_before.at(index, name),
                    "mapped cell {name} changed"
                );
            }
        }
    }
}

#[test]
fn an_unknown_clock_absence_exports_no_wall_time_even_on_the_legacy_noticed_path() {
    use musubi_reference_pipeline::export::{WALL_UNMAPPED_UNKNOWN_CLOCK, digest_full_cell};
    use musubi_reference_pipeline::{NoticedRow, WALL_MAPPED};
    use musubi_reference_readers::absence::{GapBounds, GapEnd};
    use musubi_reference_types::{ClockBasis, Mark, MarkStatus, NegativeObservation};

    let (_, mut out) = abstention_variant();
    let before = Table::of(&absences_csv(&out));
    let mapped = out
        .noticed
        .iter()
        .find(|n| n.negative.clock_basis != ClockBasis::Unknown)
        .cloned()
        .expect("the corpus must still contain a mapped row to compare against");
    assert!(!out.noticed.is_empty());

    let authored = |absent_since_ms: i64, expected_by_ms: i64, id: &str, tag: u8| NoticedRow {
        asset_id: mapped.asset_id.clone(),
        source_index: mapped.source_index,
        negative: NegativeObservation {
            subject: mapped.negative.subject.clone(),
            expectation_id: id.to_string(),
            absent_since_ms,
            expected_by_ms,
            last_good: Some(DigestRef([tag; 32])),
            clock_basis: ClockBasis::Unknown,
            time_confidence_min: 0.1,
            mark: Mark {
                status: MarkStatus::Degraded,
                reason_code: "absence-expected-cadence".into(),
                provenance: vec![],
            },
        },
        since_wall_ms: absent_since_ms,
        by_wall_ms: expected_by_ms,
        bound_ms: 0,
        bounds: GapBounds {
            end: GapEnd::NextObservationClockUnresolved,
            next_good: Some(DigestRef([tag.wrapping_add(1); 32])),
            next_good_ms: None,
            next_good_clock_basis: Some(ClockBasis::Unknown),
        },
    };
    let cases = [
        (
            1_788_166_889_500_i64,
            1_788_166_892_500_i64,
            "unknown_epoch_sized@1",
            0xA1_u8,
        ),
        (0, 3_000, "unknown_zero_relative@1", 0xB2),
        (212_500, 215_500, "unknown_relative_sized@1", 0xC3),
    ];
    let first_authored = out.noticed.len();
    for (since, by, id, tag) in cases {
        out.noticed.push(authored(since, by, id, tag));
    }

    let a = Table::of(&absences_csv(&out));
    assert_eq!(a.rows.len(), out.noticed.len() + out.unmapped.len());
    assert_eq!(a.rows.len(), before.rows.len() + cases.len());
    let digests: Vec<&str> = a.column("absence_digest_full");
    assert_eq!(
        digests
            .iter()
            .collect::<std::collections::BTreeSet<_>>()
            .len(),
        digests.len(),
        "every absence still appears exactly once"
    );

    for (offset, (since, by, id, tag)) in cases.iter().enumerate() {
        let index = first_authored + offset;
        assert_eq!(a.at(index, "expectation_id"), *id);
        for empty in WALL_CELLS {
            assert_eq!(a.at(index, empty), "", "{id}: {empty} must be empty");
        }
        assert_eq!(a.at(index, "wall_mapping"), WALL_UNMAPPED_UNKNOWN_CLOCK);
        assert_eq!(a.at(index, "absent_since_native_ms"), since.to_string());
        assert_eq!(a.at(index, "expected_by_native_ms"), by.to_string());
        assert_eq!(a.at(index, "clock_basis"), "unknown");
        assert_eq!(
            a.at(index, "last_good_digest_full"),
            digest_full_cell(&DigestRef([*tag; 32]))
        );
        assert_eq!(
            a.at(index, "next_good_digest_full"),
            digest_full_cell(&DigestRef([tag.wrapping_add(1); 32]))
        );
        assert_eq!(a.at(index, "gap_end"), "next_observation_clock_unresolved");
        assert_eq!(a.at(index, "next_good_native_ms"), "");
        assert_eq!(a.at(index, "next_good_clock_basis"), "unknown");
    }

    for row in 0..before.rows.len() {
        for name in &before.header {
            assert_eq!(
                a.at(row, name),
                before.at(row, name),
                "existing cell {name} changed"
            );
        }
    }
    let known = before
        .column("expectation_id")
        .iter()
        .position(|id| *id == mapped.negative.expectation_id)
        .expect("the mapped row must still be in the table");
    assert_eq!(a.at(known, "wall_mapping"), WALL_MAPPED);
    assert_eq!(
        a.at(known, "absent_since_wall_ms"),
        mapped.since_wall_ms.to_string()
    );
    assert_eq!(
        a.at(known, "expected_by_wall_ms"),
        mapped.by_wall_ms.to_string()
    );
    assert_eq!(a.at(known, "wall_bound_ms"), mapped.bound_ms.to_string());
    assert!(!a.at(known, "absent_since_utc").is_empty());
}

#[test]
fn mark_status_uses_the_same_snake_case_vocabulary_as_the_other_columns() {
    const LABELS: [&str; 8] = [
        "ok",
        "degraded",
        "invalid",
        "order_unknown",
        "withheld",
        "stale_revocation",
        "self_asserted_time",
        "buffer_saturated",
    ];
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    for t in [
        Table::of(&absences_csv(&out)),
        Table::of(&claims_csv(&out, &k)),
    ] {
        for v in t.column("mark_status") {
            assert!(LABELS.contains(&v), "mark_status = {v:?}");
        }
    }
}

#[test]
fn no_exported_cell_is_rewritten_by_a_spreadsheet() {
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    for text in [absences_csv(&out), claims_csv(&out, &k)] {
        assert!(text.is_ascii(), "public profile の書き出しは全文 ASCII");
        assert!(
            !text.starts_with('\u{feff}'),
            "BOM は sqlite3／jq の先頭 key を壊す"
        );
        assert!(!text.contains('\r'), "LF のみ");
        assert!(text.ends_with('\n'), "末尾改行");
        let t = Table::of(&text);
        assert_eq!(reserialize(&t), text);
        for (r, row) in t.rows.iter().enumerate() {
            for (i, cell) in row.iter().enumerate() {
                let name = t.header[i].as_str();
                for part in cell.split('|') {
                    assert!(
                        !looks_like_a_date(part),
                        "row {r} column {name} = {part:?} would be converted to a date by a spreadsheet"
                    );
                    assert!(
                        !looks_numeric_to_a_spreadsheet(part),
                        "row {r} column {name} = {part:?} would be read as a number by a spreadsheet"
                    );
                }
            }
        }
    }
}

#[test]
fn the_spreadsheet_hazard_helpers_catch_the_real_forms_and_spare_the_legitimate_ones() {
    for bad in ["2/2", "1/2", "0/0/24", "1/2/26"] {
        assert!(looks_like_a_date(bad), "{bad}");
    }
    for ok in [
        "2026-08-21T09:00:00.000Z", // absent_since_utc（日付として読まれてよい）
        "2026-08-21.1",             // catalog_version
        "never-observed",
        "channel-not-observed",
        "musubi-reference-readers@0.0.0",
        "host_received",
    ] {
        assert!(!looks_like_a_date(ok), "{ok}");
    }
    for bad in [
        "2498108893182e42", // 同梱 seed=42 sample に実在する digest（→ 2.498108893182E+54）
        "1E5",
        "12e-3",
        "007",
        "1234567890123456", // 16 桁
    ] {
        assert!(looks_numeric_to_a_spreadsheet(bad), "{bad}");
    }
    for ok in [
        "1756630860000", // epoch ms（13 桁・値は保たれる）
        "0",
        "0.20",
        "false",
        "fa536cb8fe42ee89", // 英字を含む hex は数値に読まれない
        "cause-33223d3f1557e7e5",
    ] {
        assert!(!looks_numeric_to_a_spreadsheet(ok), "{ok}");
    }
}

#[test]
fn every_digest_cell_carries_the_sha256_prefix_and_cannot_start_with_a_digit() {
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    let mut seen = 0usize;
    for t in [
        Table::of(&absences_csv(&out)),
        Table::of(&claims_csv(&out, &k)),
    ] {
        for name in DIGEST_COLUMNS {
            if !t.header.iter().any(|h| h == name) {
                continue;
            }
            for cell in t.column(name) {
                for part in cell.split('|').filter(|p| !p.is_empty()) {
                    if DIGEST_SENTINELS.contains(&part) {
                        assert!(!looks_numeric_to_a_spreadsheet(part));
                        continue;
                    }
                    assert!(part.starts_with(DIGEST_PREFIX), "{name} = {part:?}");
                    let hex = &part[DIGEST_PREFIX.len()..];
                    assert_eq!(hex.len(), 16, "{name} = {part:?}（先頭 8 byte hex）");
                    assert!(
                        hex.bytes().all(|b| b.is_ascii_hexdigit()),
                        "{name} = {part:?}"
                    );
                    assert!(
                        !part.as_bytes()[0].is_ascii_digit(),
                        "digest cell must not start with a digit: {part:?}"
                    );
                    assert!(!looks_numeric_to_a_spreadsheet(part), "{part:?}");
                    seen += 1;
                }
            }
        }
    }
    assert!(seen > 0, "the variant must produce digest cells");
}

#[test]
fn multi_value_columns_all_use_the_pipe_separator() {
    let (_, out) = abstention_variant();
    let k = Knowledge::repo_public();
    let c = Table::of(&claims_csv(&out, &k));
    for name in [
        "profile_versions",
        "channel_coverage_present",
        "channel_coverage_required",
        "evidence_digests_head",
        "indistinguishable_by",
        "absence_digests",
        "mark_reason",
    ] {
        for v in c.column(name) {
            assert!(!v.contains(','), "{name} must join with '|': {v:?}");
        }
    }
    for r in 0..c.rows.len() {
        let head = c.at(r, "evidence_digests_head");
        let n = if head.is_empty() {
            0
        } else {
            head.split('|').count()
        };
        assert!(n <= EVIDENCE_DIGESTS_HEAD);
        assert_eq!(
            n,
            c.at(r, "evidence_count")
                .parse::<usize>()
                .unwrap()
                .min(EVIDENCE_DIGESTS_HEAD)
        );
    }
}

#[test]
fn answer_text_states_question_answer_clocks_stand_in_and_ceiling() {
    let tl = generate(&pre_demo_default(42));
    let out = run_pipeline(&tl);
    let k = Knowledge::repo_public();
    let m = metrics(&tl, &out);
    let t = answer_text(&out, &k, Some(&m));
    assert!(t.contains(QUESTION_7));
    assert!(t.contains("Answer (one paragraph):"));
    assert!(t.contains("not a verdict"));
    assert!(t.contains("Clock assumptions per record"));
    assert!(t.contains("host_received"));
    assert!(t.contains("STAND-IN: KECKU public default profile"));
    assert!(t.contains("proves nothing about any partner's files"));
    assert!(t.contains("Reference metrics"));
    assert!(!t.contains('%'));
    assert_eq!(
        stand_in_lines(&out).len(),
        out.assets.iter().map(|a| a.sources.len()).sum::<usize>()
    );
}

#[test]
fn export_writes_three_files_and_only_adds_metrics_when_ground_truth_exists() {
    let tl = generate(&pre_demo_default(42));
    let out = run_pipeline(&tl);
    let k = Knowledge::repo_public();
    let base = std::env::temp_dir().join(format!("musubi-reference-export-{}", std::process::id()));
    let three = base.join("no_ground_truth");
    let w = export_all(&out, &k, None, &three).expect("export");
    assert_eq!(w.len(), 3);
    assert!(!three.join("metrics.txt").exists());
    for f in ["absences.csv", "cause_claims.csv", "answer.txt"] {
        assert!(three.join(f).is_file(), "{f}");
    }
    let four = base.join("with_ground_truth");
    let m = metrics(&tl, &out);
    let w = export_all(&out, &k, Some(&m), &four).expect("export");
    assert_eq!(w.len(), 4);
    assert!(four.join("metrics.txt").is_file());
    std::fs::remove_dir_all(&base).ok();
}

#[test]
fn collected_supporting_records_past_the_eighth_reach_the_full_export_columns() {
    use musubi_reference_pipeline::export::digest_full_cell;
    use musubi_reference_readers::cause::{SourceInput, Window, build_claim};
    use musubi_reference_readers::{FieldValue, Observation, observation_digest};
    use musubi_reference_types::{
        ClockBasis, FamilyProfile, FieldMapping, Mark, MarkStatus, NegativeObservation, Subject,
        TimeAlignment,
    };

    let obs: Vec<Observation> = (0..13_i64)
        .map(|i| {
            let t_ms = 50_000 + i * 100;
            let fields = vec![("RQly(%)".to_string(), FieldValue::F64(70.0 - i as f64))];
            Observation {
                t_ms,
                clock_basis: ClockBasis::HostReceived,
                time_confidence: 0.4,
                source_role: SourceRole::Gcs,
                channel: ChannelId::LinkStats,
                digest: observation_digest(t_ms, ChannelId::LinkStats, &fields),
                fields,
                stale: false,
                t_boot_us: None,
                anchor_unix_us: None,
                wall_ms: Some(t_ms),
            }
        })
        .collect();
    let profile = FamilyProfile {
        profile_id: "p".into(),
        version: "1".into(),
        family: Family::Ugv,
        source_role: SourceRole::Gcs,
        format: "mavlink_tlog".into(),
        extensions: vec!["tlog".into()],
        fields: FieldMapping::default(),
        field_units: std::collections::BTreeMap::new(),
        channels: vec![
            ChannelId::Heartbeat,
            ChannelId::LinkStats,
            ChannelId::Onboard,
        ],
        expectations: vec![],
        default_clock_basis: ClockBasis::HostReceived,
        declared_platform_domain: None,
        origin: "public".into(),
    };
    let neg = NegativeObservation {
        subject: Subject {
            family: Family::Ugv,
            asset_id: "a".into(),
            channel: ChannelId::Heartbeat,
        },
        expectation_id: "hb@1".into(),
        absent_since_ms: 60_000,
        expected_by_ms: 63_000,
        last_good: None,
        clock_basis: ClockBasis::HostReceived,
        time_confidence_min: 0.2,
        mark: Mark {
            status: MarkStatus::Degraded,
            reason_code: "absence-expected-cadence".into(),
            provenance: vec![],
        },
    };
    let k = Knowledge::repo_public();
    let claim = build_claim(
        &k.catalog,
        &[SourceInput {
            profile: &profile,
            obs: &obs,
            negatives: std::slice::from_ref(&neg),
            alignment: TimeAlignment {
                basis: ClockBasis::HostReceived,
                offset: None,
            },
        }],
        Family::Ugv,
        "a",
        ChannelId::Heartbeat,
        &[&neg],
        Window {
            start_ms: 60_000,
            end_ms: 90_000,
        },
        0,
    );
    let mut expected: Vec<DigestRef> = obs.iter().map(|o| o.digest).collect();
    expected.sort_by(|a, b| a.0.cmp(&b.0));
    assert_eq!(
        claim.evidence, expected,
        "collector kept every supporting record"
    );

    let (_, mut out) = abstention_variant();
    out.claims[0].claim = claim;
    let c = Table::of(&claims_csv(&out, &k));
    assert_eq!(c.at(0, "evidence_count"), "13");
    let full: Vec<&str> = c.at(0, "evidence_digests_full").split('|').collect();
    assert_eq!(full.len(), 13);
    for (actual, digest) in full.iter().zip(&expected) {
        assert_eq!(*actual, digest_full_cell(digest));
        assert_eq!(actual.len(), DIGEST_PREFIX.len() + 64);
    }
    assert_eq!(
        full.iter().collect::<std::collections::BTreeSet<_>>().len(),
        13,
        "no duplicate reference"
    );
    let head: Vec<&str> = c.at(0, "evidence_digests_head").split('|').collect();
    assert_eq!(head.len(), EVIDENCE_DIGESTS_HEAD);
    assert!(head.len() < full.len());
}
