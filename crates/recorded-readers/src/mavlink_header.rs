const HEADER_LEN: usize = 10;
const MAVLINK2_MAGIC: u8 = 0xFD;
const MAVLINK1_MAGIC: u8 = 0xFE;
#[allow(dead_code)]
#[derive(Debug)]
pub enum DecodeError {
    NotMavlink2 { magic: u8 },
    TooShort { have: usize, need: usize },
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mavlink2Header {
    pub len: u8,
    pub incompat_flags: u8,
    pub compat_flags: u8,
    pub seq: u8,
    pub sysid: u8,
    pub compid: u8,
    pub msgid: u32,
}

pub fn parse_mavlink2_header(frame: &[u8]) -> Result<Mavlink2Header, DecodeError> {
    let magic = *frame.first().ok_or(DecodeError::TooShort {
        have: frame.len(),
        need: HEADER_LEN,
    })?;
    if magic != MAVLINK2_MAGIC {
        let _ = MAVLINK1_MAGIC;
        return Err(DecodeError::NotMavlink2 { magic });
    }

    if frame.len() < HEADER_LEN {
        return Err(DecodeError::TooShort {
            have: frame.len(),
            need: HEADER_LEN,
        });
    }

    Ok(Mavlink2Header {
        len: frame[1],
        incompat_flags: frame[2],
        compat_flags: frame[3],
        seq: frame[4],
        sysid: frame[5],
        compid: frame[6],
        msgid: u32::from(frame[7]) | (u32::from(frame[8]) << 8) | (u32::from(frame[9]) << 16),
    })
}
