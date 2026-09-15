use std::path::Path;

pub use musubi_reference_types::presence::{
    HEADER, Segment, parse_csv as parse_presence_csv, render_csv,
};

use crate::{EventKind, FamilyWriter, SourceRole, Timeline, WriteError, events_for};

pub const NOMINAL_BYTES_PER_S: u64 = 2_000;

#[must_use]
pub fn segments_for(timeline: &Timeline, asset_id: &str) -> Vec<Segment> {
    let events = events_for(timeline, asset_id, SourceRole::Video);
    let mut segs: Vec<Segment> = Vec::new();
    let mut open: Option<(i64, u64)> = None; // (start_ms, seconds)
    let mut seq = 1;
    let close = |segs: &mut Vec<Segment>, start_ms: i64, secs: u64, seq: &mut u32| {
        segs.push(Segment {
            file: format!("HDZ_{seq:04}.ts"),
            size_bytes: secs * NOMINAL_BYTES_PER_S,
            mtime_unix_ms: start_ms + (secs as i64) * 1000,
            nominal_bytes_per_s: NOMINAL_BYTES_PER_S,
        });
        *seq += 1;
    };
    for e in events {
        let EventKind::VideoPresence { present } = e.kind else {
            continue;
        };
        let wall_ms = timeline.t0_unix_us / 1000 + e.t_ms as i64;
        match (present, open) {
            (true, None) => open = Some((wall_ms, 1)),
            (true, Some((s, n))) => open = Some((s, n + 1)),
            (false, Some((s, n))) => {
                close(&mut segs, s, n, &mut seq);
                open = None;
            }
            (false, None) => {}
        }
    }
    if let Some((s, n)) = open {
        close(&mut segs, s, n, &mut seq);
    }
    segs
}

#[derive(Debug, Clone, Copy, Default)]
pub struct VideoPresenceWriter;

impl FamilyWriter for VideoPresenceWriter {
    fn format_id(&self) -> &'static str {
        "video_presence"
    }
    fn extension(&self) -> &'static str {
        "csv"
    }
    fn source_role(&self) -> SourceRole {
        SourceRole::Video
    }
    fn file_label(&self) -> &'static str {
        "dvr"
    }
    fn render(&self, timeline: &Timeline, asset_id: &str) -> Result<Vec<u8>, WriteError> {
        if events_for(timeline, asset_id, SourceRole::Video).is_empty() {
            return Err(WriteError::NoEvents {
                asset_id: asset_id.to_string(),
                source: SourceRole::Video,
            });
        }
        Ok(render_csv(&segments_for(timeline, asset_id)))
    }
}

pub fn materialize_dvr_dir(dir: &Path, segments: &[Segment]) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    for g in segments {
        let path = dir.join(&g.file);
        let f = std::fs::File::create(&path)?;
        f.set_len(g.size_bytes)?;
        let mtime =
            std::time::UNIX_EPOCH + std::time::Duration::from_millis(g.mtime_unix_ms.max(0) as u64);
        f.set_modified(mtime)?;
    }
    Ok(())
}

pub fn scan_dvr_dir(dir: &Path, nominal_bytes_per_s: u64) -> std::io::Result<Vec<u8>> {
    let mut segs = Vec::new();
    let mut names: Vec<_> = std::fs::read_dir(dir)?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.extension()
                .and_then(|x| x.to_str())
                .is_some_and(|x| matches!(x.to_ascii_lowercase().as_str(), "ts" | "mp4"))
        })
        .collect();
    names.sort();
    for p in names {
        let md = std::fs::metadata(&p)?;
        let mtime = md
            .modified()?
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);
        segs.push(Segment {
            file: p
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("?")
                .to_string(),
            size_bytes: md.len(),
            mtime_unix_ms: mtime,
            nominal_bytes_per_s,
        });
    }
    Ok(render_csv(&segs))
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_scenario::{FailureKind, Family, generate, pre_demo_default};

    #[test]
    fn video_presence_writer_renders_presence_records() {
        let tl = generate(&pre_demo_default(1));
        let bytes = VideoPresenceWriter.render(&tl, "fpv-01").expect("P-03");
        assert!(!bytes.is_empty());
        let segs = parse_presence_csv(&String::from_utf8_lossy(&bytes)).expect("csv");
        assert_eq!(segs.len(), 2);
        assert_eq!(segs[0].start_unix_ms(), tl.t0_unix_us / 1000);
        assert_eq!(segs[0].mtime_unix_ms, tl.t0_unix_us / 1000 + 120_000);
        assert_eq!(segs[1].start_unix_ms(), tl.t0_unix_us / 1000 + 150_000);
    }

    #[test]
    fn dvr_dir_round_trips_through_scan_with_mtime_and_size_only() {
        let mut s = pre_demo_default(1);
        s.injections.clear();
        s.inject(FailureKind::CameraStop, 30_000, 10_000, Family::Fpv);
        let tl = generate(&s);
        let segs = segments_for(&tl, "fpv-01");
        let dir = std::env::temp_dir().join(format!("musubi-reference-dvr-{}", std::process::id()));
        materialize_dvr_dir(&dir, &segs).expect("materialize");
        let scanned = scan_dvr_dir(&dir, NOMINAL_BYTES_PER_S).expect("scan");
        std::fs::remove_dir_all(&dir).ok();
        let back = parse_presence_csv(&String::from_utf8_lossy(&scanned)).expect("csv");
        assert_eq!(back, segs);
    }
}
