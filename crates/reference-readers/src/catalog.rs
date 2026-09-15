use std::path::Path;

use musubi_reference_types::{
    ChannelId, Cue, FailureKind, FailureSignature, Family, SignatureCatalog,
};
use serde::Deserialize;

use crate::ReadError;

#[derive(Deserialize)]
struct SignatureFile {
    id: String,
    kind: String,
    #[serde(default)]
    families: Vec<String>,
    #[serde(default)]
    required: Vec<String>,
    #[serde(default)]
    optional: Vec<String>,
    #[serde(default)]
    contradicting: Vec<String>,
    #[serde(default)]
    discriminators: Vec<String>,
    consistent_with: String,
}

#[derive(Deserialize)]
struct CatalogFile {
    version: String,
    #[serde(default)]
    signature: Vec<SignatureFile>,
}

fn parse_kind(s: &str) -> Option<FailureKind> {
    Some(match s {
        "rc_link_loss" => FailureKind::RcLinkLoss,
        "telemetry_link_loss" => FailureKind::TelemetryLinkLoss,
        "fiber_break" => FailureKind::FiberBreak,
        "fc_failure" => FailureKind::FcFailure,
        "camera_stop" => FailureKind::CameraStop,
        "gnss_degradation" => FailureKind::GnssDegradation,
        "gnss_inconsistency" => FailureKind::GnssInconsistency,
        _ => return None,
    })
}

fn parse_cue(s: &str) -> Option<Cue> {
    let (ch, id) = s.split_once(':')?;
    Some(Cue {
        channel: ChannelId::parse(ch.trim())?,
        cue_id: id.trim().to_string(),
    })
}

pub fn parse_catalog(toml_str: &str) -> Result<SignatureCatalog, ReadError> {
    let f: CatalogFile = toml::from_str(toml_str).map_err(|e| ReadError::Profile(e.to_string()))?;
    let mut signatures = Vec::new();
    for s in f.signature {
        let bad = |what: &str, v: &str| {
            ReadError::Profile(format!("signature {}: unknown {what} '{v}'", s.id))
        };
        let kind = parse_kind(&s.kind).ok_or_else(|| bad("kind", &s.kind))?;
        let families = s
            .families
            .iter()
            .map(|x| Family::parse(x).ok_or_else(|| bad("family", x)))
            .collect::<Result<Vec<_>, _>>()?;
        let cues = |v: &[String]| {
            v.iter()
                .map(|x| parse_cue(x).ok_or_else(|| bad("cue", x)))
                .collect::<Result<Vec<_>, _>>()
        };
        signatures.push(FailureSignature {
            signature_id: s.id.clone(),
            kind,
            families,
            required_cues: cues(&s.required)?,
            optional_cues: cues(&s.optional)?,
            contradicting_cues: cues(&s.contradicting)?,
            discriminators: s.discriminators.clone(),
            consistent_with: s.consistent_with.clone(),
        });
    }
    Ok(SignatureCatalog {
        version: f.version,
        signatures,
    })
}

pub fn load_catalog(
    public_path: &Path,
    partner_path: Option<&Path>,
) -> Result<SignatureCatalog, ReadError> {
    let mut cat = if public_path.is_file() {
        let s = std::fs::read_to_string(public_path)
            .map_err(|e| ReadError::Profile(format!("{}: {e}", public_path.display())))?;
        parse_catalog(&s)?
    } else {
        SignatureCatalog::default()
    };
    if let Some(pp) = partner_path
        && pp.is_file()
    {
        let s = std::fs::read_to_string(pp)
            .map_err(|e| ReadError::Profile(format!("{}: {e}", pp.display())))?;
        let p = parse_catalog(&s)?;
        for sig in p.signatures {
            match cat
                .signatures
                .iter_mut()
                .find(|q| q.signature_id == sig.signature_id)
            {
                Some(slot) => *slot = sig,
                None => cat.signatures.push(sig),
            }
        }
        cat.version = format!("{}+partner:{}", cat.version, p.version);
    }
    Ok(cat)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_cues_and_rejects_unknown_channel() {
        let cat = parse_catalog(
            r#"
version = "t"
[[signature]]
id = "x"
kind = "camera_stop"
families = ["fpv"]
required = ["video:video_stop"]
contradicting = ["rc:rc_stop"]
discriminators = ["D12"]
consistent_with = "consistent with camera stop"
"#,
        )
        .expect("parses");
        assert_eq!(cat.signatures[0].required_cues[0].channel, ChannelId::Video);
        assert_eq!(cat.signatures[0].kind, FailureKind::CameraStop);
        assert!(parse_catalog("version=\"t\"\n[[signature]]\nid=\"y\"\nkind=\"camera_stop\"\nrequired=[\"nope:x\"]\nconsistent_with=\"\"\n").is_err());
    }

    #[test]
    fn partner_catalog_overrides_public_by_signature_id_and_appends_new() {
        let dir =
            std::env::temp_dir().join(format!("musubi-reference-catalog-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("mkdir");
        let pubp = dir.join("public.toml");
        let partp = dir.join("partner.toml");
        std::fs::write(&pubp, "version = \"pub\"\n[[signature]]\nid = \"x\"\nkind = \"camera_stop\"\nrequired = [\"video:video_stop\"]\nconsistent_with = \"public wording\"\n").expect("write");
        std::fs::write(&partp, "version = \"p1\"\n[[signature]]\nid = \"x\"\nkind = \"camera_stop\"\nrequired = [\"video:video_stop\", \"rc:rc_continue\"]\nconsistent_with = \"override\"\n[[signature]]\nid = \"y\"\nkind = \"fiber_break\"\nrequired = [\"rc:rc_stop\"]\nconsistent_with = \"new\"\n").expect("write");
        let cat = load_catalog(&pubp, Some(&partp)).expect("loads");
        std::fs::remove_dir_all(&dir).ok();
        assert_eq!(cat.version, "pub+partner:p1");
        assert_eq!(cat.signatures.len(), 2);
        assert_eq!(cat.signatures[0].required_cues.len(), 2);
        assert_eq!(cat.signatures[0].consistent_with, "override");
        assert_eq!(cat.signatures[1].signature_id, "y");
        assert!(
            load_catalog(&dir.join("nope.toml"), None)
                .expect("empty")
                .signatures
                .is_empty()
        );
    }
}
