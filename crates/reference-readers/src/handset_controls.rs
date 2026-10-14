use crate::{FieldValue, Observation, ReadError};
use std::collections::BTreeSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HandsetWriter {
    EdgeTx65bb97db,
    OpenTxFb8622ddTaranisHorus,
}

#[derive(Debug, Clone, Copy)]
pub enum HandsetControlKind {
    CalibratedAnalog,
    Switch,
    LogicalWord,
    LoggedPulse,
}

pub(super) fn enrich(
    mut row: Observation,
    writer: HandsetWriter,
    roles: &[(&str, HandsetControlKind)],
) -> Result<Observation, ReadError> {
    let fail = || ReadError::Profile("invalid saved handset control role or value".into());
    if roles.is_empty()
        || roles.len() > 64
        || roles.iter().map(|(n, _)| n).collect::<BTreeSet<_>>().len() != roles.len()
        || row
            .fields
            .iter()
            .any(|(n, _)| n.starts_with("reported_control."))
    {
        return Err(fail());
    }
    let mut extra = vec![];
    for (name, kind) in roles {
        if name.is_empty() {
            return Err(fail());
        }
        let value = &row
            .fields
            .iter()
            .find(|(n, _)| n == name)
            .ok_or_else(fail)?
            .1;
        let prefix = format!("reported_control.{name}");
        let mut add = |suffix: &str, v: FieldValue| extra.push((format!("{prefix}.{suffix}"), v));
        let basis = match kind {
            HandsetControlKind::CalibratedAnalog => {
                let FieldValue::I64(v) = value else {
                    return Err(fail());
                };
                add("calibrated_integer", FieldValue::I64(*v));
                "REPORTED_CALIBRATED_INTEGER_NOT_PHYSICAL_ANGLE_OR_RATIO"
            }
            HandsetControlKind::Switch => {
                let FieldValue::I64(v) = value else {
                    return Err(fail());
                };
                if !(-1..=1).contains(v) {
                    return Err(fail());
                }
                add("switch_state", FieldValue::I64(*v));
                "REPORTED_SWITCH_GETVALUE_SIGN"
            }
            HandsetControlKind::LogicalWord => {
                if *name != "LSW" {
                    return Err(fail());
                }
                let FieldValue::Text(word) = value else {
                    return Err(fail());
                };
                let digits = word
                    .strip_prefix("0x")
                    .filter(|d| d.len() == 16 && d.bytes().all(|b| b.is_ascii_hexdigit()))
                    .ok_or_else(fail)?;
                let bits = u64::from_str_radix(digits, 16).map_err(|_| fail())?;
                add("original_word", FieldValue::Text(word.clone()));
                for bit in 0..64 {
                    add(
                        &format!("bit_{bit}"),
                        FieldValue::I64(((bits >> bit) & 1) as i64),
                    );
                }
                "REPORTED_LOGICAL_SWITCH_BITS_ZERO_BASED_0_OR_1"
            }
            HandsetControlKind::LoggedPulse => {
                if writer != HandsetWriter::EdgeTx65bb97db {
                    return Err(fail());
                }
                let channel = name
                    .strip_prefix("CH")
                    .and_then(|n| n.strip_suffix("(us)"))
                    .ok_or_else(fail)?;
                if channel.is_empty()
                    || !channel.bytes().all(|b| b.is_ascii_digit())
                    || channel.starts_with('0')
                {
                    return Err(fail());
                }
                let FieldValue::I64(v) = value else {
                    return Err(fail());
                };
                // Fixed writer prints a signed int; no inferred servo or actuator range.
                if i32::try_from(*v).is_err() {
                    return Err(fail());
                }
                add("pulse_duration_s", FieldValue::F64(*v as f64 / 1_000_000.0));
                "REPORTED_LOGGED_PULSE_MICROSECONDS_NOT_COMMAND"
            }
        };
        add("basis", FieldValue::Text(basis.into()));
    }
    extra.push((
        "reported_control.writer_pin".into(),
        FieldValue::Text(
            match writer {
                HandsetWriter::EdgeTx65bb97db => "65bb97dbccbce3da6d836c885d84a32714a0012e",
                HandsetWriter::OpenTxFb8622ddTaranisHorus => {
                    "fb8622ddad710fb49908879bca96bfe1ba22f23e"
                }
            }
            .into(),
        ),
    ));
    row.fields.extend(extra);
    Ok(row)
}
