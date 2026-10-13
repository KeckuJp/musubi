use crate::{FieldValue, Observation, ProfileReader, ReadError, field, tlog::TlogReader};
use musubi_reference_types::{ClockBasis, FamilyProfile, SourceRole};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub input_bytes: usize,
    pub aggregate_read_bytes: usize,
    pub observations: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            input_bytes: 16 * 1024 * 1024,
            aggregate_read_bytes: 64 * 1024 * 1024,
            observations: 100_000,
        }
    }
}

#[derive(Debug)]
pub struct SenderOutput {
    pub profile: FamilyProfile,
    pub observations: Vec<Observation>,
    pub selected_records: usize,
    pub unselected_records: usize,
}

#[derive(Debug)]
pub struct BatchOutput {
    pub input_sha256: String,
    pub total_records: usize,
    pub selected_records: usize,
    pub unselected_records: usize,
    pub senders: Vec<SenderOutput>,
}

fn refusal(what: &str) -> ReadError {
    ReadError::Profile(what.into())
}

pub fn read(profiles: &[FamilyProfile], bytes: &[u8]) -> Result<BatchOutput, ReadError> {
    read_with_limits(profiles, bytes, Limits::default())
}

pub fn read_with_limits(
    profiles: &[FamilyProfile],
    bytes: &[u8],
    limits: Limits,
) -> Result<BatchOutput, ReadError> {
    if profiles.is_empty()
        || profiles.len() > 8
        || limits.input_bytes == 0
        || limits.aggregate_read_bytes == 0
        || limits.observations == 0
        || bytes.len() > limits.input_bytes
        || bytes
            .len()
            .checked_mul(profiles.len())
            .is_none_or(|n| n > limits.aggregate_read_bytes)
    {
        return Err(refusal("TLOG sender batch resource bound"));
    }
    let mut tuples = std::collections::BTreeSet::new();
    for profile in profiles {
        let sender = profile
            .declared_sender
            .ok_or_else(|| refusal("batch requires explicit sender"))?;
        if sender.system_id == 0
            || sender.component_id == 0
            || !tuples.insert((sender.system_id, sender.component_id))
            || profile.format != "mavlink_tlog"
            || profile.fields.time != "host_recorded_us"
            || profile.source_role != SourceRole::Gcs
            || profile.default_clock_basis != ClockBasis::Unknown
        {
            return Err(refusal(
                "batch requires distinct nonzero recorded GCS sender profiles",
            ));
        }
    }
    let mut result = BatchOutput {
        input_sha256: format!("{:x}", Sha256::digest(bytes)),
        total_records: 0,
        selected_records: 0,
        unselected_records: 0,
        senders: Vec::new(),
    };
    for profile in profiles {
        let observations = TlogReader.read(profile, bytes)?;
        let first = observations
            .first()
            .ok_or_else(|| refusal("batch sender absent"))?;
        let count = |key| match field(first, key) {
            Some(FieldValue::I64(n)) => {
                usize::try_from(*n).map_err(|_| refusal("invalid sender accounting"))
            }
            _ => Err(refusal("missing sender accounting")),
        };
        let selected = count("MAVLINK.selected_sender_records")?;
        let unselected = count("MAVLINK.unselected_sender_records")?;
        let total = selected
            .checked_add(unselected)
            .ok_or_else(|| refusal("sender count overflow"))?;
        if selected != observations.len()
            || (!result.senders.is_empty() && result.total_records != total)
        {
            return Err(refusal("inconsistent sender accounting"));
        }
        result.selected_records = result
            .selected_records
            .checked_add(selected)
            .filter(|n| *n <= limits.observations)
            .ok_or_else(|| refusal("batch observation bound"))?;
        result.total_records = total;
        result.senders.push(SenderOutput {
            profile: profile.clone(),
            observations,
            selected_records: selected,
            unselected_records: unselected,
        });
    }
    result.unselected_records = result
        .total_records
        .checked_sub(result.selected_records)
        .ok_or_else(|| refusal("overlapping sender accounting"))?;
    Ok(result)
}
