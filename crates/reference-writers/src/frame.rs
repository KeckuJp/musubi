const MAVLINK2_MAGIC: u8 = 0xFD;
const MSGID_GLOBAL_POSITION_INT: u32 = 33;
const GPI_PAYLOAD_LEN: u8 = 28;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GpiFields {
    pub time_boot_ms: u32,
    pub lat: i32,
    pub lon: i32,
    pub alt: i32,
    pub relative_alt: i32,
    pub vx: i16,
    pub vy: i16,
    pub vz: i16,
    pub hdg: u16,
}

impl Default for GpiFields {
    fn default() -> Self {
        Self {
            time_boot_ms: 1000,
            lat: 356_762_000,
            lon: 1_396_503_000,
            alt: 100_000,
            relative_alt: 100_000,
            vx: 0,
            vy: 0,
            vz: 0,
            hdg: 9000,
        }
    }
}

impl GpiFields {
    #[must_use]
    pub fn at(lat_deg: f64, lon_deg: f64, alt_m: f64) -> Self {
        Self {
            lat: (lat_deg * 1e7) as i32,
            lon: (lon_deg * 1e7) as i32,
            alt: (alt_m * 1000.0) as i32,
            relative_alt: (alt_m * 1000.0) as i32,
            ..Self::default()
        }
    }
}

#[must_use]
pub fn build_global_position_int_frame(seq: u8, f: GpiFields) -> Vec<u8> {
    let mut frame = Vec::with_capacity(40);
    frame.push(MAVLINK2_MAGIC); // [0] magic
    frame.push(GPI_PAYLOAD_LEN); // [1] len = 28
    frame.push(0x00); // [2] incompat_flags（署名なし）
    frame.push(0x00); // [3] compat_flags
    frame.push(seq); // [4] seq
    frame.push(0x01); // [5] sysid
    frame.push(0x01); // [6] compid
    let msgid = MSGID_GLOBAL_POSITION_INT;
    frame.push((msgid & 0xFF) as u8);
    frame.push(((msgid >> 8) & 0xFF) as u8);
    frame.push(((msgid >> 16) & 0xFF) as u8);
    frame.extend_from_slice(&f.time_boot_ms.to_le_bytes());
    frame.extend_from_slice(&f.lat.to_le_bytes());
    frame.extend_from_slice(&f.lon.to_le_bytes());
    frame.extend_from_slice(&f.alt.to_le_bytes());
    frame.extend_from_slice(&f.relative_alt.to_le_bytes());
    frame.extend_from_slice(&f.vx.to_le_bytes());
    frame.extend_from_slice(&f.vy.to_le_bytes());
    frame.extend_from_slice(&f.vz.to_le_bytes());
    frame.extend_from_slice(&f.hdg.to_le_bytes());
    frame.push(0x00);
    frame.push(0x00);
    frame
}
