use std::io::{self, BufRead, BufReader, Cursor, Read};

use musubi_reference_readers::{
    ProfileReader,
    edgetx::{EdgeTxCsvReader, HandsetStreamFailure},
    profile::parse_profile,
};

const BOOT: &str = include_str!("fixtures/edgetx-handset-clock--boot-counter.profile.toml");
const RTC: &str = include_str!("fixtures/edgetx-handset-clock--rtc-date-time.profile.toml");

#[test]
fn empty_invalid_header_and_missing_sensor_are_refused_without_emission() {
    let profile = parse_profile(BOOT, "public").unwrap();
    for text in [
        "",
        "tmr10ms,RQly(%)\n",
        "tmr10ms,RQly(%),RQly(%)\n1,2,3\n",
        "tmr10ms,unknown\n1,2\n",
        "tmr10ms,RQly(%)\n1,\0\n",
    ] {
        let result =
            EdgeTxCsvReader.read_stream(&profile, &mut Cursor::new(text.as_bytes()), |_| {
                panic!("no accepted record")
            });
        assert!(matches!(
            result.unwrap_err().reason,
            HandsetStreamFailure::Decode(_)
        ));
    }
    let mut wrong = profile.clone();
    wrong.format = "telemetry_csv_us".into();
    let failure = EdgeTxCsvReader
        .read_stream(&wrong, &mut Cursor::new(b"tmr10ms,RQly(%)\n1,2\n"), |_| {
            panic!("wrong profile")
        })
        .unwrap_err();
    assert_eq!(failure.counts.bytes_read, 0);
}

#[test]
fn streamed_observations_are_identical_to_whole_reader_including_digest() {
    for (profile, bytes) in [
        (
            BOOT,
            "tmr10ms,RQly(%),Text,Unknown,Blank\r\n3000,100,\"ready,正常\",9007199254740993,\r\n3000,0,\"0012\",-2,",
        ),
        (
            RTC,
            "Date,Time,RQly(%),Unknown\n2026-08-31,09:00:00.000,100,hello\n2026-08-31,09:00:00.010,0,world\n",
        ),
    ] {
        let profile = parse_profile(profile, "public").unwrap();
        let expected = EdgeTxCsvReader.read(&profile, bytes.as_bytes()).unwrap();
        let mut input = BufReader::with_capacity(1, Cursor::new(bytes.as_bytes()));
        let mut actual = Vec::new();
        let counts = EdgeTxCsvReader
            .read_stream(&profile, &mut input, |record| {
                actual.push(record);
                Ok(())
            })
            .unwrap();
        assert_eq!(actual, expected);
        assert_eq!(counts.bytes_read, bytes.len() as u64);
        assert_eq!(counts.records_read, 2);
        assert_eq!(counts.emitted_records, 2);
    }
}

struct RepeatedRows {
    row: Vec<u8>,
    remaining: usize,
    offset: usize,
}
impl Read for RepeatedRows {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let available = self.fill_buf()?;
        let n = output.len().min(available.len());
        output[..n].copy_from_slice(&available[..n]);
        self.consume(n);
        Ok(n)
    }
}
impl BufRead for RepeatedRows {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        Ok(if self.remaining == 0 {
            &[]
        } else {
            &self.row[self.offset..]
        })
    }
    fn consume(&mut self, amount: usize) {
        self.offset += amount;
        if self.offset == self.row.len() {
            self.offset = 0;
            self.remaining -= 1;
        }
    }
}

#[test]
fn ordinary_input_larger_than_sixteen_mib_emits_every_record_without_collecting() {
    let profile = parse_profile(BOOT, "public").unwrap();
    let header = b"tmr10ms,RQly(%),Unknown\n";
    let row = format!("3000,100,{}\n", "x".repeat(1024)).into_bytes();
    let expected_bytes = header.len() + row.len() * 17000;
    assert!(expected_bytes > 16 * 1024 * 1024);
    let mut input = Cursor::new(header).chain(RepeatedRows {
        row,
        remaining: 17000,
        offset: 0,
    });
    let mut emitted = 0;
    let counts = EdgeTxCsvReader
        .read_stream(&profile, &mut input, |record| {
            assert_eq!(record.t_ms, 30000);
            assert_eq!(record.fields.len(), 3);
            emitted += 1;
            Ok(())
        })
        .unwrap();
    assert_eq!(emitted, 17000);
    assert_eq!(counts.records_read, 17000);
    assert_eq!(counts.emitted_records, 17000);
    assert_eq!(counts.bytes_read, expected_bytes as u64);
    assert!(
        EdgeTxCsvReader
            .read(&profile, &vec![b'x'; 16 * 1024 * 1024 + 1])
            .is_err()
    );
}

#[test]
fn reset_and_partial_csv_are_loud_after_prior_emission() {
    let profile = parse_profile(BOOT, "public").unwrap();
    for tail in [
        "2999,100",
        "3001",
        "3001,\"unterminated",
        "\n",
        "3001,nan\n",
    ] {
        let input = format!("tmr10ms,RQly(%)\n3000,100\n{tail}");
        let mut emitted = 0;
        let failure = EdgeTxCsvReader
            .read_stream(&profile, &mut Cursor::new(input.as_bytes()), |_| {
                emitted += 1;
                Ok(())
            })
            .unwrap_err();
        assert!(
            matches!(failure.reason, HandsetStreamFailure::Decode(_)),
            "{tail}"
        );
        assert_eq!(emitted, 1);
        assert_eq!(failure.counts.emitted_records, 1);
        assert_eq!(failure.counts.records_read, 2);
        assert_eq!(failure.counts.bytes_read, input.len() as u64);
        assert_eq!(failure.line_number, 3);
    }
}

#[test]
fn bounded_line_refusal_does_not_drain_unbounded_input() {
    let profile = parse_profile(BOOT, "public").unwrap();
    let header = "tmr10ms,RQly(%)\n";
    let input = format!("{header}{}\n3000,100\n", "x".repeat(200000));
    let mut cursor = Cursor::new(input.as_bytes());
    let failure = EdgeTxCsvReader
        .read_stream(&profile, &mut cursor, |_| panic!("no valid row"))
        .unwrap_err();
    assert!(matches!(failure.reason, HandsetStreamFailure::Limit(_)));
    assert_eq!(failure.counts.records_read, 0);
    assert_eq!(failure.counts.emitted_records, 0);
    assert_eq!(failure.counts.bytes_read, header.len() as u64 + 65537);
    assert_eq!(cursor.position(), failure.counts.bytes_read);
    assert_eq!(failure.line_number, 2);
}

struct FailsAfterBytes {
    source: Cursor<Vec<u8>>,
}
impl Read for FailsAfterBytes {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        let available = self.fill_buf()?;
        let n = out.len().min(available.len());
        out[..n].copy_from_slice(&available[..n]);
        self.consume(n);
        Ok(n)
    }
}
impl BufRead for FailsAfterBytes {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        if self.source.position() == self.source.get_ref().len() as u64 {
            Err(io::Error::other("authored read failure"))
        } else {
            self.source.fill_buf()
        }
    }
    fn consume(&mut self, n: usize) {
        self.source.consume(n);
    }
}

#[test]
fn io_and_callback_failure_preserve_acknowledged_counts() {
    let profile = parse_profile(BOOT, "public").unwrap();
    let bytes = b"tmr10ms,RQly(%)\n3000,100\n300".to_vec();
    let expected_bytes = bytes.len() as u64;
    let failure = EdgeTxCsvReader
        .read_stream(
            &profile,
            &mut FailsAfterBytes {
                source: Cursor::new(bytes),
            },
            |_| Ok(()),
        )
        .unwrap_err();
    assert!(matches!(failure.reason, HandsetStreamFailure::Input(_)));
    assert_eq!(failure.counts.bytes_read, expected_bytes);
    assert_eq!(failure.counts.records_read, 1);
    assert_eq!(failure.counts.emitted_records, 1);
    assert_eq!(failure.line_number, 3);

    let mut cursor = Cursor::new(b"tmr10ms,RQly(%)\n3000,100\n3001,0\n3002,100\n");
    let mut calls = 0;
    let failure = EdgeTxCsvReader
        .read_stream(&profile, &mut cursor, |_| {
            calls += 1;
            if calls == 2 {
                Err("authored output failure".into())
            } else {
                Ok(())
            }
        })
        .unwrap_err();
    assert!(matches!(failure.reason, HandsetStreamFailure::Output(_)));
    assert_eq!(calls, 2);
    assert_eq!(failure.counts.records_read, 2);
    assert_eq!(failure.counts.emitted_records, 1);
    assert_eq!(failure.line_number, 3);
    assert_eq!(cursor.position(), failure.counts.bytes_read);
}
