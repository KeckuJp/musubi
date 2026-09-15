#![allow(clippy::expect_used, clippy::missing_const_for_fn)]

use std::path::PathBuf;
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

use musubi_field_contracts::{ObservationValueV1, RecordV1, decode_ndjson};

const WIRE: &[u8] = &[
    0xfd, 0x0e, 0, 0, 9, 1, 42, 132, 0, 0, 0x40, 0xe2, 1, 0, 2, 0, 0x90, 1, 37, 0, 1, 7, 0, 255,
    0xbc, 0x2f,
];

struct Capture(PathBuf);

impl Capture {
    fn new(bytes: &[u8]) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "musubi-range-cli-{}-{}.capture",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .expect("unique synthetic capture");
        std::io::Write::write_all(&mut file, bytes).expect("write synthetic fixture");
        Self(path)
    }
}

impl Drop for Capture {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn framed(messages: &[&[u8]]) -> Vec<u8> {
    let mut bytes = vec![];
    for message in messages {
        bytes.extend_from_slice(
            &u32::try_from(message.len())
                .expect("fixture size")
                .to_be_bytes(),
        );
        bytes.extend_from_slice(message);
    }
    bytes
}

fn run(binary: &PathBuf, capture: &Capture, trailing: &[&str]) -> Output {
    Command::new(binary)
        .arg(&capture.0)
        .args(trailing)
        .output()
        .expect("offline example process")
}

#[test]
fn offline_receipt_cli_is_bounded_filtered_and_honest_about_rejections() {
    let build = Command::new(env!("CARGO"))
        .args([
            "build",
            "--quiet",
            "-p",
            "musubi-field-contracts",
            "--example",
            "mavlink_range_receipt",
        ])
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .output()
        .expect("build offline receipt example");
    assert!(
        build.status.success(),
        "{}",
        String::from_utf8_lossy(&build.stderr)
    );
    let binary = std::env::current_exe()
        .expect("test executable")
        .parent()
        .expect("deps")
        .parent()
        .expect("debug")
        .join("examples")
        .join(format!(
            "mavlink_range_receipt{}",
            std::env::consts::EXE_SUFFIX
        ));

    let capture = Capture::new(&framed(&[WIRE]));
    let result = run(&binary, &capture, &["fixture-source", "1", "42"]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let records = decode_ndjson(&result.stdout).expect("only valid NDJSON on stdout");
    assert_eq!(records.len(), 1);
    let RecordV1::ObservationExtension(value) = &records[0] else {
        panic!("observation")
    };
    assert_eq!(value.value, Some(ObservationValueV1::U64(37)));
    assert_eq!(value.source.source_id, "fixture-source/sys-1/comp-42");
    let diagnostic = String::from_utf8_lossy(&result.stderr);
    for counter in [
        "received=1",
        "mapped=1",
        "unmapped=0",
        "foreign=0",
        "rejected=0",
    ] {
        assert!(
            diagnostic.split_whitespace().any(|token| token == counter),
            "missing {counter}: {diagnostic}"
        );
    }

    let mut foreign = WIRE.to_vec();
    foreign[5] = 2;
    let mut unmapped = WIRE.to_vec();
    unmapped[7] = 33;
    let mixed = Capture::new(&framed(&[&foreign, &unmapped, WIRE]));
    let result = run(&binary, &mixed, &["fixture-source", "1", "42"]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(decode_ndjson(&result.stdout).expect("NDJSON").len(), 1);
    let diagnostic = String::from_utf8_lossy(&result.stderr);
    for counter in [
        "received=3",
        "mapped=1",
        "unmapped=1",
        "foreign=1",
        "rejected=0",
    ] {
        assert!(
            diagnostic.split_whitespace().any(|token| token == counter),
            "missing {counter}: {diagnostic}"
        );
    }

    let mut bad_crc = WIRE.to_vec();
    bad_crc[18] ^= 1;
    let partial = Capture::new(&framed(&[WIRE, &bad_crc]));
    let result = run(&binary, &partial, &["fixture-source", "1", "42"]);
    assert!(
        !result.status.success(),
        "rejected records must not produce successful receipt"
    );
    let diagnostic = String::from_utf8_lossy(&result.stderr);
    assert!(
        diagnostic
            .split_whitespace()
            .any(|token| token == "rejected=1"),
        "{diagnostic}"
    );
    assert!(decode_ndjson(&result.stdout).is_ok());

    for bytes in [
        vec![],
        vec![0, 0, 0],
        vec![0, 0, 0, 0],
        vec![0, 1, 0, 0],
        framed(&[&WIRE[..WIRE.len() - 1]]),
        framed(&[&foreign]),
    ] {
        let malformed = Capture::new(&bytes);
        assert!(
            !run(&binary, &malformed, &["fixture-source", "1", "42"])
                .status
                .success(),
            "empty/unmapped/malformed capture must fail"
        );
    }
    let oversized = Capture::new(&vec![0; 16 * 1024 * 1024 + 1]);
    assert!(
        !run(&binary, &oversized, &["fixture-source", "1", "42"])
            .status
            .success()
    );

    for args in [
        vec![],
        vec!["source"],
        vec!["source", "1"],
        vec!["source", "1", "42", "extra"],
        vec!["source", "256", "42"],
        vec!["source", "1", "-1"],
        vec!["", "1", "42"],
    ] {
        let result = run(&binary, &capture, &args);
        assert!(!result.status.success(), "invalid arguments {args:?}");
        assert!(
            result.stdout.is_empty(),
            "invalid args must not emit candidate records"
        );
    }
}
