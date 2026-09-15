use std::collections::BTreeMap;
use std::path::Path;

use musubi_reference_types::{
    ChannelId, ClockBasis, ExpectationModel, Family, FamilyProfile, FieldMapping, SourceRole,
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
    fields: FieldsFile,
    #[serde(default)]
    units: BTreeMap<String, String>,
    #[serde(default)]
    expectations: Vec<ExpectationFile>,
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
                if !valid_threshold
                    || names.is_empty()
                    || names.len() > 32
                    || names
                        .iter()
                        .any(|n| n.is_empty() || n.len() > 128 || n.trim() != n)
                    || names
                        .iter()
                        .collect::<std::collections::BTreeSet<_>>()
                        .len()
                        != names.len()
                {
                    return Err(ReadError::Profile(
                        "invalid explicit frozen field selection".into(),
                    ));
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
        origin: origin.to_string(),
    })
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
