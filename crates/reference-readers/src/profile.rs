use std::collections::BTreeMap;
use std::path::Path;

use musubi_reference_types::{
    ChannelId, ClockBasis, ClockRateConfig, ExpectationModel, Family, FamilyProfile, FieldMapping,
    SenderSelection, SourceRole,
};
use musubi_types::PlatformDomain;
use serde::Deserialize;

use crate::ReadError;

#[derive(Debug, serde::Serialize)]
pub struct ProfileSourceIdentity {
    pub profile_id: String,
    pub declared_version: String,
    pub source_sha256: String,
}

pub fn parse_profile_with_identity(
    source: &str,
    origin: &str,
) -> Result<(FamilyProfile, ProfileSourceIdentity), ReadError> {
    use sha2::{Digest, Sha256};
    let profile = parse_profile(source, origin)?;
    let identity = ProfileSourceIdentity {
        profile_id: profile.profile_id.clone(),
        declared_version: profile.version.clone(),
        source_sha256: format!("sha256:{:x}", Sha256::digest(source.as_bytes())),
    };
    Ok((profile, identity))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FieldsFile {
    time: String,
    #[serde(default)]
    status: Vec<String>,
    #[serde(default)]
    link: Vec<String>,
    #[serde(default)]
    battery: Vec<String>,
    #[serde(default)]
    gps: Vec<String>,
    #[serde(default)]
    health: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExpectationFile {
    id: String,
    channel: String,
    cadence_ms: u64,
    grace_k: u32,
    #[serde(default)]
    frozen_ms: Option<u64>,
    #[serde(default)]
    frozen_fields: Option<Vec<String>>,
    #[serde(default)]
    required_fields: Option<Vec<String>>,
    #[serde(default)]
    window_rule: Option<String>,
}

const WINDOW_RULE_DECLARED_WALL: &str = "declared_wall_window";

fn valid_field_selection(names: &[String]) -> bool {
    !names.is_empty()
        && names.len() <= 32
        && names
            .iter()
            .all(|n| !n.is_empty() && n.len() <= 128 && n.trim() == n)
        && names
            .iter()
            .collect::<std::collections::BTreeSet<_>>()
            .len()
            == names.len()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileFile {
    profile_id: String,
    version: String,
    family: String,
    source_role: String,
    format: String,
    extensions: Vec<String>,
    default_clock_basis: String,
    #[serde(default)]
    channels: Vec<String>,
    #[serde(default)]
    platform_domain: Option<String>,
    #[serde(default)]
    clock_rate: Option<ClockRateFile>,
    #[serde(default)]
    selected_sender: Option<SenderFile>,
    fields: FieldsFile,
    #[serde(default)]
    units: BTreeMap<String, String>,
    #[serde(default)]
    expectations: Vec<ExpectationFile>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ClockRateFile {
    anchor_uncertainty_us: i64,
    min_span_us: i64,
    max_abs_rate_ppm: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SenderFile {
    system_id: u8,
    component_id: u8,
}

pub fn parse_profile(toml_str: &str, origin: &str) -> Result<FamilyProfile, ReadError> {
    let f: ProfileFile = toml::from_str(toml_str).map_err(|e| ReadError::Profile(e.to_string()))?;
    let bad =
        |what: &str, v: &str| ReadError::Profile(format!("{}: unknown {what} '{v}'", f.profile_id));
    let family = Family::parse(&f.family).ok_or_else(|| bad("family", &f.family))?;
    let source_role =
        SourceRole::parse(&f.source_role).ok_or_else(|| bad("source_role", &f.source_role))?;
    let default_clock_basis = ClockBasis::parse(&f.default_clock_basis)
        .ok_or_else(|| bad("clock_basis", &f.default_clock_basis))?;
    let declared_platform_domain = declared_domain(&f, family, &bad)?;
    let declared_clock_rate = declared_clock_rate(&f)?;
    let declared_sender = declared_sender(&f)?;
    let channels = f
        .channels
        .iter()
        .map(|c| ChannelId::parse(c).ok_or_else(|| bad("channel", c)))
        .collect::<Result<Vec<_>, _>>()?;
    let expectations = f
        .expectations
        .iter()
        .map(|e| {
            if let Some(names) = &e.frozen_fields {
                let valid_threshold = e
                    .frozen_ms
                    .is_some_and(|ms| ms > 0 && ms <= i64::MAX as u64);
                if !valid_threshold || !valid_field_selection(names) {
                    return Err(ReadError::Profile(
                        "invalid explicit frozen field selection".into(),
                    ));
                }
            }
            if let Some(names) = &e.required_fields {
                if !valid_field_selection(names) {
                    return Err(ReadError::Profile(
                        "invalid explicit required field selection".into(),
                    ));
                }
                if e.frozen_fields.is_some() {
                    return Err(ReadError::Profile(format!(
                        "{}: expectation '{}' declares both required_fields and frozen_fields; \
                         which one admits an observation would be decided silently",
                        f.profile_id, e.id
                    )));
                }
                if !f.channels.iter().any(|c| c == &e.channel) {
                    return Err(ReadError::Profile(format!(
                        "{}: expectation '{}' requires fields on channel '{}', which this profile \
                         does not declare in channels",
                        f.profile_id, e.id, e.channel
                    )));
                }
            }
            if let Some(rule) = &e.window_rule {
                if rule != WINDOW_RULE_DECLARED_WALL {
                    return Err(ReadError::Profile(format!(
                        "{}: expectation '{}' has unknown window_rule '{rule}'",
                        f.profile_id, e.id
                    )));
                }
                if e.required_fields.is_none() {
                    return Err(ReadError::Profile(format!(
                        "{}: expectation '{}' declares window_rule without required_fields",
                        f.profile_id, e.id
                    )));
                }
            }
            Ok(ExpectationModel {
                expectation_id: e.id.clone(),
                channel: ChannelId::parse(&e.channel)
                    .ok_or_else(|| bad("expectation channel", &e.channel))?,
                cadence_ms: e.cadence_ms,
                grace_k: e.grace_k,
                frozen_ms: e.frozen_ms,
                frozen_fields: e.frozen_fields.clone(),
                required_fields: e.required_fields.clone(),
                declared_window_only: e.window_rule.is_some(),
            })
        })
        .collect::<Result<Vec<_>, ReadError>>()?;
    Ok(FamilyProfile {
        profile_id: f.profile_id,
        version: f.version,
        family,
        source_role,
        format: f.format,
        extensions: f
            .extensions
            .iter()
            .map(|e| e.trim_start_matches('.').to_ascii_lowercase())
            .collect(),
        fields: FieldMapping {
            time: f.fields.time,
            status: f.fields.status,
            link: f.fields.link,
            battery: f.fields.battery,
            gps: f.fields.gps,
            health: f.fields.health,
        },
        field_units: f.units,
        channels,
        expectations,
        default_clock_basis,
        declared_platform_domain,
        declared_clock_rate,
        declared_sender,
        origin: origin.to_string(),
    })
}

const CLOCK_RATE_FORMATS: [(&str, i64); 1] = [("ardupilot_dataflash_bin", 1_000)];

fn declared_clock_rate(f: &ProfileFile) -> Result<Option<ClockRateConfig>, ReadError> {
    let Some(c) = &f.clock_rate else {
        return Ok(None);
    };
    let refuse = |why: &str| {
        Err(ReadError::Profile(format!(
            "{}: clock_rate {why}",
            f.profile_id
        )))
    };
    let Some((_, floor_us)) = CLOCK_RATE_FORMATS
        .iter()
        .find(|(name, _)| *name == f.format.as_str())
    else {
        return refuse(&format!(
            "is not honoured by format '{}'; it produces no paired in-log anchors",
            f.format
        ));
    };
    if c.anchor_uncertainty_us < *floor_us {
        return refuse(&format!(
            "needs a declared anchor_uncertainty_us of at least {floor_us} us for format '{}': its \
             reference reading is quantized that coarsely, so a finer half-width would claim a \
             precision the recording does not contain. This floor only refuses an impossible \
             precision - it is not evidence that the pairing latency is bounded, and the real \
             pairing error stays the operator's to declare in full",
            f.format
        ));
    }
    if c.min_span_us < 1 {
        return refuse("needs a declared positive min_span_us");
    }
    if c.max_abs_rate_ppm < 1 {
        return refuse(
            "needs a declared positive max_abs_rate_ppm; no universal acceptable clock rate is supplied",
        );
    }
    Ok(Some(ClockRateConfig {
        anchor_uncertainty_us: c.anchor_uncertainty_us,
        min_span_us: c.min_span_us,
        max_abs_rate_ppm: c.max_abs_rate_ppm,
    }))
}

const SENDER_SELECTING_FORMATS: [&str; 1] = ["mavlink_tlog"];

fn declared_sender(f: &ProfileFile) -> Result<Option<SenderSelection>, ReadError> {
    let Some(s) = &f.selected_sender else {
        return Ok(None);
    };
    let refuse = |why: &str| {
        Err(ReadError::Profile(format!(
            "{}: selected_sender {why}",
            f.profile_id
        )))
    };
    if !SENDER_SELECTING_FORMATS.contains(&f.format.as_str()) {
        return refuse(&format!(
            "is not honoured by format '{}'; its records carry no reported sender tuple",
            f.format
        ));
    }
    if s.system_id == 0 || s.component_id == 0 {
        return refuse(
            "needs a nonzero system_id and component_id; a zero id is not an addressable recorded \
             sender and would select nothing",
        );
    }
    Ok(Some(SenderSelection {
        system_id: s.system_id,
        component_id: s.component_id,
    }))
}

const DOMAIN_DECLARING_FORMATS: [&str; 1] = ["telemetry_csv_us"];

fn declared_domain(
    f: &ProfileFile,
    family: Family,
    bad: &impl Fn(&str, &str) -> ReadError,
) -> Result<Option<PlatformDomain>, ReadError> {
    let Some(label) = f.platform_domain.as_deref() else {
        return Ok(None);
    };
    if !DOMAIN_DECLARING_FORMATS.contains(&f.format.as_str()) {
        return Err(ReadError::Profile(format!(
            "{}: format '{}' has no reading path that carries a declared platform_domain '{label}'",
            f.profile_id, f.format
        )));
    }
    if family != Family::Unknown {
        return Err(ReadError::Profile(format!(
            "{}: platform_domain '{label}' cannot re-declare the domain of family '{}'",
            f.profile_id,
            family.as_str()
        )));
    }
    match label {
        "air" => Ok(Some(PlatformDomain::Air)),
        "surface" => Ok(Some(PlatformDomain::Surface)),
        "ground" => Ok(Some(PlatformDomain::Ground)),
        "unknown" => Ok(Some(PlatformDomain::Unknown)),
        other => Err(bad("platform_domain", other)),
    }
}

pub fn load_profile_dir(dir: &Path, origin: &str) -> Result<Vec<FamilyProfile>, ReadError> {
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut names: Vec<_> = std::fs::read_dir(dir)
        .map_err(|e| ReadError::Profile(format!("{}: {e}", dir.display())))?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "toml"))
        .collect();
    names.sort();
    names
        .iter()
        .map(|p| {
            let s = std::fs::read_to_string(p)
                .map_err(|e| ReadError::Profile(format!("{}: {e}", p.display())))?;
            parse_profile(&s, origin)
        })
        .collect()
}

pub fn load_profiles(
    public_dir: &Path,
    partner_dir: Option<&Path>,
) -> Result<Vec<FamilyProfile>, ReadError> {
    let mut out = load_profile_dir(public_dir, "public")?;
    if let Some(pd) = partner_dir {
        for p in load_profile_dir(pd, "partner")? {
            match out.iter_mut().find(|q| q.profile_id == p.profile_id) {
                Some(slot) => *slot = p,
                None => out.push(p),
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_identity_tracks_actual_bytes_not_only_declared_revision() {
        let (_, first) = parse_profile_with_identity(MINIMAL, "development").unwrap();
        let (_, second) =
            parse_profile_with_identity(&format!("{MINIMAL}\n# comment\n"), "development").unwrap();
        assert_eq!(first.profile_id, "demo_tlog");
        assert_eq!(first.declared_version, second.declared_version);
        assert_ne!(first.source_sha256, second.source_sha256);
        assert!(parse_profile_with_identity("invalid profile", "development").is_err());
    }

    const MINIMAL: &str = r#"
profile_id = "demo_tlog"
version = "0.1"
family = "ugv"
source_role = "gcs"
format = "mavlink_tlog"
extensions = [".tlog"]
default_clock_basis = "host_received"
channels = ["heartbeat", "link_stats"]
[fields]
time = "host_us"
link = ["RADIO_STATUS.rssi"]
[[expectations]]
id = "hb"
channel = "heartbeat"
cadence_ms = 1000
grace_k = 3
frozen_ms = 20000
"#;

    #[test]
    fn parse_minimal_profile_normalises_extension_and_origin() {
        let p = parse_profile(MINIMAL, "public").expect("parses");
        assert_eq!(p.extensions, vec!["tlog"]);
        assert_eq!(p.origin, "public");
        assert_eq!(p.default_clock_basis, ClockBasis::HostReceived);
        assert_eq!(p.expectations[0].channel, ChannelId::Heartbeat);
        assert_eq!(p.expectations[0].frozen_ms, Some(20_000));
    }

    #[test]
    fn units_are_preserved_exactly_in_deterministic_key_order() {
        let with_units = MINIMAL.replace(
            "[[expectations]]",
            "[units]\n\"RADIO_STATUS.rssi\" = \"dBm\"\n\"GPS_RAW_INT.lat\" = \"deg_e7\"\n\n[[expectations]]",
        );
        let p = parse_profile(&with_units, "public").expect("parses");
        assert_eq!(
            p.field_units,
            BTreeMap::from([
                ("GPS_RAW_INT.lat".to_string(), "deg_e7".to_string()),
                ("RADIO_STATUS.rssi".to_string(), "dBm".to_string()),
            ])
        );
    }

    #[test]
    fn unknown_top_level_and_fields_keys_are_rejected() {
        let top_level = MINIMAL.replace(
            "channels = [\"heartbeat\", \"link_stats\"]",
            "channels = [\"heartbeat\", \"link_stats\"]\nunsupported = true",
        );
        assert!(matches!(
            parse_profile(&top_level, "public"),
            Err(ReadError::Profile(_))
        ));

        let fields = MINIMAL.replace(
            "link = [\"RADIO_STATUS.rssi\"]",
            "link = [\"RADIO_STATUS.rssi\"]\nunsupported = [\"x\"]",
        );
        assert!(matches!(
            parse_profile(&fields, "public"),
            Err(ReadError::Profile(_))
        ));
    }

    #[test]
    fn unknown_label_is_rejected_fail_closed() {
        let bad = MINIMAL.replace("host_received", "wall");
        assert!(matches!(
            parse_profile(&bad, "public"),
            Err(ReadError::Profile(_))
        ));
    }

    #[test]
    fn partner_profile_overrides_public_by_id() {
        let dir =
            std::env::temp_dir().join(format!("musubi-reference-profiles-{}", std::process::id()));
        let pubd = dir.join("public");
        let partd = dir.join("partner");
        std::fs::create_dir_all(&pubd).expect("mkdir");
        std::fs::create_dir_all(&partd).expect("mkdir");
        std::fs::write(pubd.join("a.toml"), MINIMAL).expect("write");
        std::fs::write(
            partd.join("b.toml"),
            MINIMAL.replace("version = \"0.1\"", "version = \"9.9\""),
        )
        .expect("write");
        let ps = load_profiles(&pubd, Some(&partd)).expect("loads");
        assert_eq!(ps.len(), 1);
        assert_eq!(ps[0].version, "9.9");
        assert_eq!(ps[0].origin, "partner");
        let only_public = load_profiles(&pubd, None).expect("loads");
        assert_eq!(only_public[0].origin, "public");
        std::fs::remove_dir_all(&dir).ok();
    }
}
