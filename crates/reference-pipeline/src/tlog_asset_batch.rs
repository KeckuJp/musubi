use crate::{InputFile, Knowledge, ObservationWindow, PipelineOut, analyze};
use musubi_reference_readers::tlog_sender_batch::{self, BatchOutput, Limits};
use musubi_reference_types::{ClockBasis, FamilyProfile, SignatureCatalog};
use std::collections::BTreeSet;

#[derive(Debug, Clone)]
pub struct Binding {
    pub asset_id: String,
    pub profile: FamilyProfile,
    pub host_received_basis: String,
}

#[derive(Debug)]
pub struct Output {
    pub reported: BatchOutput,
    pub bindings: Vec<Binding>,
    pub analyses: Vec<PipelineOut>,
}

pub fn analyze_declared(
    bindings: &[Binding],
    bytes: &[u8],
    catalog: &SignatureCatalog,
    window: ObservationWindow,
    limits: Limits,
) -> Result<Output, String> {
    analyze_bound(bindings, bytes, catalog, window, limits, false)
}

#[derive(Debug, Clone)]
pub struct RecordedBinding {
    pub asset_id: String,
    pub profile: FamilyProfile,
}

#[derive(Debug)]
pub struct RecordedOutput {
    pub reported: BatchOutput,
    pub bindings: Vec<RecordedBinding>,
    pub analyses: Vec<PipelineOut>,
}

pub fn analyze_recorded(
    bindings: &[RecordedBinding],
    bytes: &[u8],
    catalog: &SignatureCatalog,
    recorded_start_ms: i64,
    recorded_end_ms: i64,
    limits: Limits,
) -> Result<RecordedOutput, String> {
    if recorded_start_ms < 0 || recorded_end_ms < recorded_start_ms {
        return Err("invalid recorded-axis interval".into());
    }
    let internal: Vec<_> = bindings
        .iter()
        .map(|binding| Binding {
            asset_id: binding.asset_id.clone(),
            profile: binding.profile.clone(),
            host_received_basis: String::new(),
        })
        .collect();
    let out = analyze_bound(
        &internal,
        bytes,
        catalog,
        ObservationWindow::Explicit {
            t0_ms: recorded_start_ms,
            end_ms: recorded_end_ms,
        },
        limits,
        true,
    )?;
    Ok(RecordedOutput {
        reported: out.reported,
        bindings: bindings.to_vec(),
        analyses: out.analyses,
    })
}

fn analyze_bound(
    bindings: &[Binding],
    bytes: &[u8],
    catalog: &SignatureCatalog,
    window: ObservationWindow,
    limits: Limits,
    recorded: bool,
) -> Result<Output, String> {
    let mut labels = BTreeSet::new();
    if bindings.is_empty()
        || bindings.len() > 8
        || bindings.iter().any(|binding| {
            binding.asset_id.trim().is_empty()
                || binding.asset_id.len() > 256
                || binding.asset_id.chars().any(char::is_control)
                || !labels.insert(binding.asset_id.as_str())
                || if recorded {
                    binding.profile.fields.time != "host_recorded_us"
                        || binding.profile.default_clock_basis != ClockBasis::Unknown
                } else {
                    binding.host_received_basis.trim().is_empty()
                        || binding.host_received_basis.len() > 4096
                        || binding.profile.fields.time != "host_us"
                        || binding.profile.default_clock_basis != ClockBasis::HostReceived
                }
        })
        || bytes
            .len()
            .checked_mul(bindings.len())
            .and_then(|n| n.checked_mul(2))
            .is_none_or(|n| n > limits.aggregate_read_bytes)
    {
        return Err("invalid declared asset batch or aggregate read bound".into());
    }
    let profiles: Vec<_> = bindings
        .iter()
        .map(|binding| {
            let mut profile = binding.profile.clone();
            profile.fields.time = "host_recorded_us".into();
            profile.default_clock_basis = ClockBasis::Unknown;
            profile
        })
        .collect();
    let reported = tlog_sender_batch::read_with_limits(&profiles, bytes, limits)
        .map_err(|error| format!("declared sender preflight: {error}"))?;
    let mut analyses = Vec::with_capacity(bindings.len());
    for binding in bindings {
        let result = analyze(
            vec![InputFile {
                asset_id: binding.asset_id.clone(),
                family: binding.profile.family,
                source: binding.profile.source_role,
                format_id: binding.profile.format.clone(),
                file_name: "declared-saved-input.tlog".into(),
                bytes: bytes.to_vec(),
            }],
            &Knowledge {
                profiles: vec![binding.profile.clone()],
                catalog: catalog.clone(),
            },
            window,
        )?;
        analyses.push(result);
    }
    Ok(Output {
        reported,
        bindings: bindings.to_vec(),
        analyses,
    })
}
