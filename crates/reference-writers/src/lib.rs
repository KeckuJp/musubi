#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::too_many_lines,
    clippy::missing_const_for_fn,
    clippy::module_name_repetitions
)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

pub mod bin;
pub mod blackbox;
pub mod edgetx;
pub mod frame;
pub mod mavlink;
pub mod tlog;
pub mod ulog;
pub mod video;

pub use musubi_reference_scenario::{Event, EventKind, Timeline};
pub use musubi_reference_types::{Family, SourceRole};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WriteError {
    NotImplemented {
        format: &'static str,
        ledger: &'static str,
    },
    NoEvents {
        asset_id: String,
        source: SourceRole,
    },
    FormatMismatch {
        asset_id: String,
        format: &'static str,
    },
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotImplemented { format, ledger } => {
                write!(f, "writer for {format} is not implemented yet ({ledger})")
            }
            Self::NoEvents { asset_id, source } => {
                write!(
                    f,
                    "no events for asset {asset_id} / source {}",
                    source.as_str()
                )
            }
            Self::FormatMismatch { asset_id, format } => {
                write!(f, "asset {asset_id} does not record {format}")
            }
        }
    }
}

impl std::error::Error for WriteError {}

pub trait FamilyWriter {
    fn format_id(&self) -> &'static str;
    fn extension(&self) -> &'static str;
    fn source_role(&self) -> SourceRole;
    fn file_label(&self) -> &'static str {
        self.source_role().as_str()
    }
    fn render(&self, timeline: &Timeline, asset_id: &str) -> Result<Vec<u8>, WriteError>;
}

#[must_use]
pub fn events_for<'a>(
    timeline: &'a Timeline,
    asset_id: &str,
    source: SourceRole,
) -> Vec<&'a Event> {
    let mut v: Vec<&Event> = timeline
        .events
        .iter()
        .filter(|e| e.asset_id == asset_id && e.source == source)
        .collect();
    v.sort_by_key(|e| e.t_ms);
    v
}

pub fn fc_events_for<'a>(
    timeline: &'a Timeline,
    asset_id: &str,
    expected: musubi_reference_scenario::FcLogFormat,
    format: &'static str,
) -> Result<Vec<&'a Event>, WriteError> {
    let asset = timeline.asset(asset_id);
    if asset.is_none_or(|a| a.fc_log != expected || !a.sources.contains(&SourceRole::Fc)) {
        return Err(WriteError::FormatMismatch {
            asset_id: asset_id.to_string(),
            format,
        });
    }
    let events = events_for(timeline, asset_id, SourceRole::Fc);
    if events.is_empty() {
        return Err(WriteError::NoEvents {
            asset_id: asset_id.to_string(),
            source: SourceRole::Fc,
        });
    }
    Ok(events)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RenderedFile {
    pub asset_id: String,
    pub family: Family,
    pub source: SourceRole,
    pub format_id: &'static str,
    pub file_name: String,
    pub bytes: Vec<u8>,
}

#[must_use]
pub fn render_all(
    timeline: &Timeline,
    assets: &[(String, Family)],
    writers: &[&dyn FamilyWriter],
) -> (Vec<RenderedFile>, Vec<(String, &'static str, WriteError)>) {
    let mut out = Vec::new();
    let mut skipped = Vec::new();
    for (asset_id, family) in assets {
        for w in writers {
            if *family == Family::Unknown {
                skipped.push((
                    asset_id.clone(),
                    w.format_id(),
                    WriteError::FormatMismatch {
                        asset_id: asset_id.clone(),
                        format: "known-family synthetic record",
                    },
                ));
                continue;
            }
            match w.render(timeline, asset_id) {
                Ok(bytes) => out.push(RenderedFile {
                    asset_id: asset_id.clone(),
                    family: *family,
                    source: w.source_role(),
                    format_id: w.format_id(),
                    file_name: format!("{asset_id}_{}.{}", w.file_label(), w.extension()),
                    bytes,
                }),
                Err(e) => skipped.push((asset_id.clone(), w.format_id(), e)),
            }
        }
    }
    (out, skipped)
}

#[must_use]
pub fn default_writers() -> Vec<Box<dyn FamilyWriter>> {
    vec![
        Box::new(tlog::TlogWriter),
        Box::new(edgetx::EdgeTxCsvWriter),
        Box::new(bin::ArduPilotBinWriter),
        Box::new(ulog::Px4UlgWriter),
        Box::new(blackbox::BlackboxCsvWriter),
        Box::new(video::VideoPresenceWriter),
    ]
}
