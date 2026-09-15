use std::fmt;

pub type ContractResult<T> = Result<T, ContractError>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContractError {
    MissingField(&'static str),
    InvalidField { field: &'static str, reason: String },
    CommandShapedIdentifier(String),
    DuplicateRegistryKey(String),
    SilentRedefinition(String),
    Json(String),
    NdjsonLine { line: usize, reason: String },
}

impl fmt::Display for ContractError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingField(field) => write!(f, "missing required field: {field}"),
            Self::InvalidField { field, reason } => write!(f, "invalid {field}: {reason}"),
            Self::CommandShapedIdentifier(identifier) => {
                write!(f, "command-shaped identifier is forbidden: {identifier}")
            }
            Self::DuplicateRegistryKey(key) => write!(f, "duplicate registry key: {key}"),
            Self::SilentRedefinition(key) => {
                write!(f, "client overlay silently redefines public key: {key}")
            }
            Self::Json(reason) => write!(f, "JSON error: {reason}"),
            Self::NdjsonLine { line, reason } => {
                write!(f, "NDJSON line {line} is invalid: {reason}")
            }
        }
    }
}

impl std::error::Error for ContractError {}

impl From<serde_json::Error> for ContractError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value.to_string())
    }
}

pub trait Validate {
    fn validate(&self) -> ContractResult<()>;
}

pub fn require_non_empty(value: &str, field: &'static str) -> ContractResult<()> {
    if value.trim().is_empty() {
        Err(ContractError::MissingField(field))
    } else {
        Ok(())
    }
}

pub fn require_schema_v1(value: &str) -> ContractResult<()> {
    if value == crate::SCHEMA_VERSION_V1 {
        Ok(())
    } else {
        Err(ContractError::InvalidField {
            field: "schema_version",
            reason: format!("expected {}, got {value}", crate::SCHEMA_VERSION_V1),
        })
    }
}

pub fn require_finite(value: f64, field: &'static str) -> ContractResult<()> {
    if value.is_finite() {
        Ok(())
    } else {
        Err(ContractError::InvalidField {
            field,
            reason: "number must be finite".to_owned(),
        })
    }
}
