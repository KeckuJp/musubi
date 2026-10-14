//! Local byte-only JSON projections for typed host-state evidence.

use std::collections::BTreeMap;

use musubi_types::{
    ComObject, EvidenceEnvelope, HostStateCounter, HostStateNetworkInterface, HostStateObservation,
    Mark,
};

use crate::{DialectCapability, NorthboundPublisher, PublishError, mark_status_label};

#[derive(Debug, Clone, Copy, Default)]
pub struct EvidenceNdjsonPublisher;

impl EvidenceNdjsonPublisher {
    #[must_use]
    pub const fn new() -> Self {
        Self
    }
}

impl NorthboundPublisher for EvidenceNdjsonPublisher {
    fn dialect(&self) -> &str {
        "evidence-ndjson"
    }

    fn capability(&self) -> DialectCapability {
        native_mark_capability()
    }

    fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
        let observation = host_state(envelope, self.dialect())?;
        let digest = digest(envelope, self.dialect())?;
        let mut output = String::new();
        output.push_str("{\"schema\":\"musubi.evidence.ndjson/v1\",\"observation\":");
        push_observation(&mut output, observation);
        output.push_str(",\"claim\":{\"claim_id\":");
        push_string(&mut output, &envelope.claim.claim_id);
        output.push_str(",\"confidence\":");
        push_f32(&mut output, envelope.claim.confidence);
        output.push_str(",\"mark\":");
        push_mark(&mut output, &envelope.claim.mark);
        output.push_str("},\"classification\":");
        push_option_string(&mut output, envelope.classification.as_deref());
        output.push_str(",\"content_digest\":");
        push_string(&mut output, &digest);
        output.push_str("}\n");
        Ok(output.into_bytes())
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct MaintenanceHostStateNdjsonPublisher;

impl MaintenanceHostStateNdjsonPublisher {
    #[must_use]
    pub const fn new() -> Self {
        Self
    }
}

impl NorthboundPublisher for MaintenanceHostStateNdjsonPublisher {
    fn dialect(&self) -> &str {
        "maintenance-host-state-ndjson"
    }

    fn capability(&self) -> DialectCapability {
        native_mark_capability()
    }

    fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
        let observation = host_state(envelope, self.dialect())?;
        let digest = digest(envelope, self.dialect())?;
        let mut output = String::new();
        output.push_str("{\"schema\":\"musubi.maintenance.host-state.ndjson/v1\",\"evidence_id\":");
        push_string(&mut output, &envelope.claim.claim_id);
        output.push_str(",\"content_digest\":");
        push_string(&mut output, &digest);
        output.push_str(",\"host_state\":");
        push_observation(&mut output, observation);
        output.push_str(",\"mark\":");
        push_mark(&mut output, &envelope.claim.mark);
        output.push_str(",\"confidence\":");
        push_f32(&mut output, envelope.claim.confidence);
        output.push_str("}\n");
        Ok(output.into_bytes())
    }
}

const fn native_mark_capability() -> DialectCapability {
    DialectCapability {
        carries_position: false,
        carries_mark_status: true,
        mark_via_native_annotation: false,
    }
}

fn host_state<'a>(
    envelope: &'a EvidenceEnvelope,
    dialect: &str,
) -> Result<&'a HostStateObservation, PublishError> {
    match &envelope.observation {
        ComObject::HostStateObservation(observation) => Ok(observation),
        ComObject::PlatformState(_) => Err(PublishError {
            dialect: dialect.to_string(),
            reason: "unsupported non-host observation".to_string(),
        }),
    }
}

fn digest(envelope: &EvidenceEnvelope, dialect: &str) -> Result<String, PublishError> {
    let digest = envelope.content_digest.ok_or_else(|| PublishError {
        dialect: dialect.to_string(),
        reason: "unsealed host-state evidence".to_string(),
    })?;
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        encoded.push(hex_digit(byte >> 4));
        encoded.push(hex_digit(byte & 0x0f));
    }
    Ok(encoded)
}

const fn hex_digit(value: u8) -> char {
    match value {
        0..=9 => (b'0' + value) as char,
        _ => (b'a' + value - 10) as char,
    }
}

fn push_observation(output: &mut String, observation: &HostStateObservation) {
    output.push_str("{\"kind\":\"host_state_observation\",\"source_contract_id\":");
    push_string(output, &observation.source_contract_id);
    output.push_str(",\"host_id\":");
    push_string(output, &observation.host_id);
    output.push_str(",\"sequence\":");
    output.push_str(&observation.sequence.to_string());
    output.push_str(",\"timestamps\":{\"observed_at\":");
    push_option_i64(output, observation.timestamps.observed_at);
    output.push_str(",\"received_at\":");
    output.push_str(&observation.timestamps.received_at.to_string());
    output.push_str(",\"time_confidence\":");
    push_f32(output, observation.timestamps.time_confidence);
    output.push_str("},\"host\":{\"os\":{\"id\":");
    push_option_string(output, observation.host.os.id.as_deref());
    output.push_str(",\"version_id\":");
    push_option_string(output, observation.host.os.version_id.as_deref());
    output.push_str("},\"kernel_release\":");
    push_option_string(output, observation.host.kernel_release.as_deref());
    output.push_str(",\"model\":");
    push_option_string(output, observation.host.model.as_deref());
    output.push_str(",\"uptime_seconds\":");
    push_option_f64(output, observation.host.uptime_seconds);
    output.push_str(",\"load\":{\"one_min\":");
    push_option_f64(output, observation.host.load.one_min);
    output.push_str(",\"five_min\":");
    push_option_f64(output, observation.host.load.five_min);
    output.push_str(",\"fifteen_min\":");
    push_option_f64(output, observation.host.load.fifteen_min);
    output.push_str("},\"memory_kib\":{\"mem_total\":");
    push_option_u64(output, observation.host.memory_kib.mem_total);
    output.push_str(",\"mem_available\":");
    push_option_u64(output, observation.host.memory_kib.mem_available);
    output.push_str(",\"swap_total\":");
    push_option_u64(output, observation.host.memory_kib.swap_total);
    output.push_str(",\"swap_free\":");
    push_option_u64(output, observation.host.memory_kib.swap_free);
    output.push_str("},\"cpu_total_ticks\":");
    push_counters(output, &observation.host.cpu_total_ticks);
    output.push_str("},\"network\":");
    push_network(output, &observation.network);
    output.push('}');
}

fn push_network(output: &mut String, network: &BTreeMap<String, HostStateNetworkInterface>) {
    output.push('{');
    for (index, (name, interface)) in network.iter().enumerate() {
        if index > 0 {
            output.push(',');
        }
        push_string(output, name);
        output.push_str(":{\"ifindex\":");
        push_option_u32(output, interface.ifindex);
        output.push_str(",\"operstate\":");
        push_option_string(output, interface.operstate.as_deref());
        output.push_str(",\"carrier\":");
        push_option_bool(output, interface.carrier);
        output.push_str(",\"mtu\":");
        push_option_u32(output, interface.mtu);
        output.push_str(",\"counters\":");
        push_counters(output, &interface.counters);
        output.push('}');
    }
    output.push('}');
}

fn push_counters(output: &mut String, counters: &BTreeMap<String, HostStateCounter>) {
    output.push('{');
    for (index, (name, counter)) in counters.iter().enumerate() {
        if index > 0 {
            output.push(',');
        }
        push_string(output, name);
        output.push_str(":{\"raw\":");
        output.push_str(&counter.raw.to_string());
        output.push_str(",\"delta\":");
        push_option_u64(output, counter.delta);
        output.push('}');
    }
    output.push('}');
}

fn push_mark(output: &mut String, mark: &Mark) {
    output.push_str("{\"status\":");
    push_string(output, mark_status_label(mark.status));
    output.push_str(",\"reason_code\":");
    push_string(output, &mark.reason_code);
    output.push_str(",\"provenance\":[");
    for (index, item) in mark.provenance.iter().enumerate() {
        if index > 0 {
            output.push(',');
        }
        push_string(output, item);
    }
    output.push_str("]}");
}

fn push_string(output: &mut String, value: &str) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value <= '\u{1f}' => {
                output.push_str("\\u00");
                let byte = value as u8;
                output.push(hex_digit(byte >> 4));
                output.push(hex_digit(byte & 0x0f));
            }
            value => output.push(value),
        }
    }
    output.push('"');
}

fn push_option_string(output: &mut String, value: Option<&str>) {
    match value {
        Some(value) => push_string(output, value),
        None => output.push_str("null"),
    }
}

fn push_option_i64(output: &mut String, value: Option<i64>) {
    match value {
        Some(value) => output.push_str(&value.to_string()),
        None => output.push_str("null"),
    }
}

fn push_option_u64(output: &mut String, value: Option<u64>) {
    match value {
        Some(value) => output.push_str(&value.to_string()),
        None => output.push_str("null"),
    }
}

fn push_option_u32(output: &mut String, value: Option<u32>) {
    match value {
        Some(value) => output.push_str(&value.to_string()),
        None => output.push_str("null"),
    }
}

fn push_option_bool(output: &mut String, value: Option<bool>) {
    match value {
        Some(true) => output.push_str("true"),
        Some(false) => output.push_str("false"),
        None => output.push_str("null"),
    }
}

fn push_option_f64(output: &mut String, value: Option<f64>) {
    match value {
        Some(value) => push_f64(output, value),
        None => output.push_str("null"),
    }
}

fn push_f64(output: &mut String, value: f64) {
    if value.is_finite() {
        output.push_str(&value.to_string());
    } else {
        output.push_str("null");
    }
}

fn push_f32(output: &mut String, value: f32) {
    if value.is_finite() {
        output.push_str(&value.to_string());
    } else {
        output.push_str("null");
    }
}
