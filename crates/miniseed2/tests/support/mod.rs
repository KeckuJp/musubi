// Synthetic SEED 2.4 appendix B fixture; no real station bytes.
pub fn put16(b: &mut [u8], at: usize, v: u16) {
    b[at..at + 2].copy_from_slice(&v.to_be_bytes());
}
pub fn put32(b: &mut [u8], at: usize, v: u32) {
    b[at..at + 4].copy_from_slice(&v.to_be_bytes());
}
pub fn record(encoding: u8) -> Vec<u8> {
    let mut b = vec![0; 256];
    b[..20].copy_from_slice(b"000001D SYN01  BHZXX");
    put16(&mut b, 20, 2024);
    put16(&mut b, 22, 60);
    b[24] = 1;
    b[25] = 2;
    b[26] = 3;
    put16(&mut b, 28, 1234);
    put16(&mut b, 30, 4);
    put16(&mut b, 32, 20);
    put16(&mut b, 34, 1);
    b[39] = 2;
    put16(&mut b, 44, 64);
    put16(&mut b, 46, 48);
    put16(&mut b, 48, 1000);
    put16(&mut b, 50, 56);
    b[52] = encoding;
    b[53] = 1;
    b[54] = 8;
    put16(&mut b, 56, 1001);
    b[60] = 100;
    b[63] = 1;
    put32(&mut b, 64, 1 << 24); // word 3: four signed 8-bit differences
    put32(&mut b, 68, 100);
    put32(&mut b, 72, 103);
    put32(&mut b, 76, 0x4902fd04); // d0=73 is ignored; +2,-3,+4
    b
}

pub fn packed_record(
    encoding: u8,
    width: u32,
    count: usize,
    code: u32,
    dnib: Option<u32>,
) -> (Vec<u8>, Vec<i32>) {
    let mut diffs = vec![0_i64, -(1_i64 << (width - 1)), (1_i64 << (width - 1)) - 1];
    while diffs.len() % count != 0 {
        diffs.push(0);
    }
    let mut expected = vec![0_i32];
    for d in &diffs[1..] {
        expected.push((i64::from(*expected.last().unwrap()) + d) as i32);
    }
    let mut b = record(encoding);
    b[64..].fill(0);
    put16(&mut b, 30, expected.len() as u16);
    put32(&mut b, 72, *expected.last().unwrap() as u32);
    let mut control = 0;
    for (i, chunk) in diffs.chunks(count).enumerate() {
        let mut word = 0_u64;
        for d in chunk {
            word = (word << width) | ((*d as u64) & ((1_u64 << width) - 1));
        }
        if let Some(n) = dnib {
            word |= u64::from(n) << 30;
        }
        control |= code << (30 - 2 * (i + 3));
        put32(&mut b, 64 + 4 * (i + 3), word as u32);
    }
    put32(&mut b, 64, control);
    (b, expected)
}
