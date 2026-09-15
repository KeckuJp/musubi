use serde::{Deserialize, Serialize};

use crate::{
    CableInteractionProfileV1, ContractError, ContractResult, EpisodeAnnotationV1, HumanSealV1,
    IntegrationCaseManifestV1, ObservationExtensionV1, RegistryV1, Validate, VerificationReportV1,
};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "record_type", content = "record", rename_all = "snake_case")]
pub enum RecordV1 {
    ObservationExtension(ObservationExtensionV1),
    EpisodeAnnotation(EpisodeAnnotationV1),
    CableInteractionProfile(CableInteractionProfileV1),
    IntegrationCaseManifest(IntegrationCaseManifestV1),
    VerificationReport(VerificationReportV1),
    HumanSeal(HumanSealV1),
    Registry(RegistryV1),
}

impl Validate for RecordV1 {
    fn validate(&self) -> ContractResult<()> {
        match self {
            Self::ObservationExtension(value) => value.validate(),
            Self::EpisodeAnnotation(value) => value.validate(),
            Self::CableInteractionProfile(value) => value.validate(),
            Self::IntegrationCaseManifest(value) => value.validate(),
            Self::VerificationReport(value) => value.validate(),
            Self::HumanSeal(value) => value.validate(),
            Self::Registry(value) => value.validate(),
        }
    }
}

pub fn encode_ndjson(records: &[RecordV1]) -> ContractResult<Vec<u8>> {
    let mut output = Vec::new();
    for record in records {
        record.validate()?;
        serde_json::to_writer(&mut output, record)?;
        output.push(b'\n');
    }
    Ok(output)
}

pub fn decode_ndjson(input: &[u8]) -> ContractResult<Vec<RecordV1>> {
    let text =
        std::str::from_utf8(input).map_err(|error| ContractError::Json(error.to_string()))?;
    let mut records = Vec::new();
    for (index, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            return Err(ContractError::NdjsonLine {
                line: index + 1,
                reason: "empty records are forbidden".to_owned(),
            });
        }
        let record: RecordV1 =
            serde_json::from_str(line).map_err(|error| ContractError::NdjsonLine {
                line: index + 1,
                reason: error.to_string(),
            })?;
        record
            .validate()
            .map_err(|error| ContractError::NdjsonLine {
                line: index + 1,
                reason: error.to_string(),
            })?;
        records.push(record);
    }
    Ok(records)
}
