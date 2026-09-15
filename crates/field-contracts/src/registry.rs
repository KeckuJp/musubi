use std::collections::BTreeMap;
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::error::{require_non_empty, require_schema_v1};
use crate::{
    ArtifactReferenceV1, ContractError, ContractResult, DigestV1, EpisodeAnnotationV1,
    ObservationExtensionV1, ObservationKindV1, PrimitiveValueKindV1, Validate,
};

const BANNED_TOKENS: &[&str] = &[
    "command",
    "commands",
    "cmd",
    "execute",
    "dispatch",
    "route",
    "actuate",
    "actuation",
    "destination",
];
const BANNED_COMPACT: &[&str] = &[
    "recommendedaction",
    "actionrecommendation",
    "desiredstate",
    "sendcommand",
    "injectcommand",
    "overridecontrol",
    "uploadmission",
    "deploypayload",
];

pub fn lint_identifier(identifier: &str) -> ContractResult<()> {
    require_non_empty(identifier, "identifier")?;
    if !identifier
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.' | ':' | '/'))
    {
        return Err(ContractError::InvalidField {
            field: "identifier",
            reason: "only ASCII letters, digits, _, -, ., :, and / are allowed".to_owned(),
        });
    }

    let lower = identifier.to_ascii_lowercase();
    let tokens: Vec<_> = lower
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|token| !token.is_empty())
        .collect();
    let compact: String = lower.chars().filter(char::is_ascii_alphanumeric).collect();
    if tokens.iter().any(|token| BANNED_TOKENS.contains(token))
        || BANNED_COMPACT
            .iter()
            .any(|forbidden| compact.contains(forbidden))
    {
        return Err(ContractError::CommandShapedIdentifier(
            identifier.to_owned(),
        ));
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RegistryKindV1 {
    Observation,
    DerivedFeature,
    AnnotationLabel,
}

impl fmt::Display for RegistryKindV1 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Observation => f.write_str("observation"),
            Self::DerivedFeature => f.write_str("derived_feature"),
            Self::AnnotationLabel => f.write_str("annotation_label"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegistryKeyV1 {
    pub kind: RegistryKindV1,
    pub namespace: String,
    pub semantic_id: String,
    pub semantic_version: String,
}

impl fmt::Display for RegistryKeyV1 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}|{}|{}|{}",
            self.kind, self.namespace, self.semantic_id, self.semantic_version
        )
    }
}

impl Validate for RegistryKeyV1 {
    fn validate(&self) -> ContractResult<()> {
        lint_identifier(&self.namespace)?;
        lint_identifier(&self.semantic_id)?;
        require_non_empty(&self.semantic_version, "registry.semantic_version")
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegistryEntryV1 {
    pub key: RegistryKeyV1,
    pub value_kind: PrimitiveValueKindV1,
    pub unit: Option<String>,
    pub description: String,
    pub provenance: ArtifactReferenceV1,
    pub deprecated: bool,
}

impl Validate for RegistryEntryV1 {
    fn validate(&self) -> ContractResult<()> {
        self.key.validate()?;
        if let Some(unit) = &self.unit {
            require_non_empty(unit, "registry.unit")?;
        }
        require_non_empty(&self.description, "registry.description")?;
        self.provenance.validate()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "scope", rename_all = "snake_case")]
pub enum RegistryScopeV1 {
    Public,
    ClientOverlay {
        tenant_id: String,
        rights_digest: DigestV1,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegistryV1 {
    pub schema_version: String,
    pub scope: RegistryScopeV1,
    pub entries: Vec<RegistryEntryV1>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum ObservationRegistryOutcomeV1 {
    KnownValidated { key: RegistryKeyV1 },
    UnknownPreserved { key: RegistryKeyV1 },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum AnnotationRegistryOutcomeV1 {
    KnownValidated { key: RegistryKeyV1 },
    UnknownPreserved { key: RegistryKeyV1 },
}

impl RegistryV1 {
    #[must_use]
    pub fn resolve(&self, key: &RegistryKeyV1) -> Option<&RegistryEntryV1> {
        self.entries.iter().find(|entry| &entry.key == key)
    }

    pub fn validate_observation(
        &self,
        observation: &ObservationExtensionV1,
    ) -> ContractResult<ObservationRegistryOutcomeV1> {
        self.validate()?;
        observation.validate()?;
        let key = RegistryKeyV1 {
            kind: match observation.observation_kind {
                ObservationKindV1::Raw => RegistryKindV1::Observation,
                ObservationKindV1::Derived => RegistryKindV1::DerivedFeature,
            },
            namespace: observation.namespace.clone(),
            semantic_id: observation.semantic_id.clone(),
            semantic_version: observation.semantic_version.clone(),
        };
        let Some(entry) = self.resolve(&key) else {
            return Ok(ObservationRegistryOutcomeV1::UnknownPreserved { key });
        };
        if observation.value_kind != entry.value_kind {
            return Err(ContractError::InvalidField {
                field: "observation.value_kind",
                reason: "does not match the known registry value_kind".to_owned(),
            });
        }
        if observation.unit != entry.unit {
            return Err(ContractError::InvalidField {
                field: "observation.unit",
                reason: "does not match the known registry unit".to_owned(),
            });
        }
        Ok(ObservationRegistryOutcomeV1::KnownValidated { key })
    }

    pub fn validate_annotation(
        &self,
        annotation: &EpisodeAnnotationV1,
    ) -> ContractResult<AnnotationRegistryOutcomeV1> {
        self.validate()?;
        annotation.validate()?;
        let key = RegistryKeyV1 {
            kind: RegistryKindV1::AnnotationLabel,
            namespace: annotation.taxonomy_namespace.clone(),
            semantic_id: annotation.label.clone(),
            semantic_version: annotation.taxonomy_version.clone(),
        };
        let Some(entry) = self.resolve(&key) else {
            return Ok(AnnotationRegistryOutcomeV1::UnknownPreserved { key });
        };
        if entry.value_kind != PrimitiveValueKindV1::Text || entry.unit.is_some() {
            return Err(ContractError::InvalidField {
                field: "annotation.label",
                reason: "known annotation labels must be registered as unitless text".to_owned(),
            });
        }
        Ok(AnnotationRegistryOutcomeV1::KnownValidated { key })
    }

    pub fn overlay(public: &Self, client: &Self) -> ContractResult<Self> {
        public.validate()?;
        client.validate()?;
        if !matches!(public.scope, RegistryScopeV1::Public) {
            return Err(ContractError::InvalidField {
                field: "registry.public.scope",
                reason: "base registry must have public scope".to_owned(),
            });
        }
        if !matches!(client.scope, RegistryScopeV1::ClientOverlay { .. }) {
            return Err(ContractError::InvalidField {
                field: "registry.client.scope",
                reason: "overlay registry must have client_overlay scope".to_owned(),
            });
        }

        let mut merged: BTreeMap<RegistryKeyV1, RegistryEntryV1> = public
            .entries
            .iter()
            .cloned()
            .map(|entry| (entry.key.clone(), entry))
            .collect();
        for entry in &client.entries {
            if let Some(public_entry) = merged.get(&entry.key) {
                if public_entry != entry {
                    return Err(ContractError::SilentRedefinition(entry.key.to_string()));
                }
            } else {
                merged.insert(entry.key.clone(), entry.clone());
            }
        }

        Ok(Self {
            schema_version: crate::SCHEMA_VERSION_V1.to_owned(),
            scope: client.scope.clone(),
            entries: merged.into_values().collect(),
        })
    }
}

impl Validate for RegistryV1 {
    fn validate(&self) -> ContractResult<()> {
        require_schema_v1(&self.schema_version)?;
        if let RegistryScopeV1::ClientOverlay { tenant_id, .. } = &self.scope {
            require_non_empty(tenant_id, "registry.scope.tenant_id")?;
        }
        let mut keys = BTreeMap::new();
        for entry in &self.entries {
            entry.validate()?;
            if keys.insert(&entry.key, ()).is_some() {
                return Err(ContractError::DuplicateRegistryKey(entry.key.to_string()));
            }
        }
        Ok(())
    }
}
