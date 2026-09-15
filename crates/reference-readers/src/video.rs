use musubi_reference_types::presence::parse_csv as parse_presence_csv;
use musubi_reference_types::{ChannelId, FamilyProfile};

use crate::{FieldValue, Observation, ProfileReader, ReadError, observation};

#[derive(Debug, Clone, Copy, Default)]
pub struct VideoPresenceReader;

impl ProfileReader for VideoPresenceReader {
    fn format_id(&self) -> &'static str {
        "video_presence"
    }
    fn read(&self, profile: &FamilyProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        let text = std::str::from_utf8(bytes).map_err(|e| ReadError::Malformed {
            offset: e.valid_up_to(),
            what: "not utf-8".into(),
        })?;
        let segs = parse_presence_csv(text).map_err(|ln| ReadError::Malformed {
            offset: ln,
            what: "bad presence csv row".into(),
        })?;
        let mut out = Vec::new();
        for g in segs {
            let start = g.start_unix_ms();
            let mut t = start;
            while t < g.mtime_unix_ms {
                let fields = vec![
                    ("file".to_string(), FieldValue::Text(g.file.clone())),
                    (
                        "size_bytes".to_string(),
                        FieldValue::I64(g.size_bytes as i64),
                    ),
                    (
                        "mtime_unix_ms".to_string(),
                        FieldValue::I64(g.mtime_unix_ms),
                    ),
                ];
                let mut o = observation(profile, t, ChannelId::Video, fields, false);
                o.wall_ms = Some(t);
                out.push(o);
                t += 1_000;
            }
        }
        out.sort_by_key(|o| o.t_ms);
        Ok(out)
    }
}
