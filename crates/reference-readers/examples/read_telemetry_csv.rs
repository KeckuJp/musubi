use musubi_reference_readers::{
    FieldValue, profile::parse_profile_with_identity, telemetry_csv::TelemetryCsvReader,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() < 3
        || args[3..].iter().any(|a| {
            !matches!(
                a.as_str(),
                "--allow-equal-time" | "--preserve-nonfinite-as-text"
            )
        })
    {
        return Err(
            "usage: read_telemetry_csv PROFILE.toml CONVERTED.csv [--allow-equal-time] [--preserve-nonfinite-as-text]".into(),
        );
    }
    let allow_equal = args[3..].iter().any(|a| a == "--allow-equal-time");
    let preserve_nonfinite = args[3..]
        .iter()
        .any(|a| a == "--preserve-nonfinite-as-text");
    let options = musubi_decoded_csv::ParseOptions {
        allow_equal_time: allow_equal,
        preserve_nonfinite_as_text: preserve_nonfinite,
    };
    let (profile, profile_identity) =
        parse_profile_with_identity(&std::fs::read_to_string(&args[1])?, "development")
            .map_err(|e| format!("profile: {e:?}"))?;
    let bytes = std::fs::read(&args[2])?;
    let report = TelemetryCsvReader
        .read_report_with_options(&profile, &bytes, options)
        .map_err(|e| format!("input: {e:?}"))?;
    let observations: Vec<_> = report.observations.iter().map(|o| {
        let fields: serde_json::Map<String, Value> = o.fields.iter().map(|(key, value)| {
            let v = match value {
                FieldValue::I64(n) => json!(n), FieldValue::F64(n) => json!(n),
                FieldValue::Text(s) => json!(s), FieldValue::Blank => Value::Null,
            };
            (key.clone(), v)
        }).collect();
        json!({"t_ms":o.t_ms,"t_boot_us":o.t_boot_us,"clock_basis":format!("{:?}",o.clock_basis),
               "wall_ms":o.wall_ms,"anchor_unix_us":o.anchor_unix_us,"time_confidence":o.time_confidence,
               "channel":o.channel.as_str(),"fields":fields})
    }).collect();
    let (platform_domain, domain_source, domain_basis) = match profile.declared_platform_domain {
        Some(domain) => (
            format!("{domain:?}"),
            "profile_declared_platform_domain",
            "explicit profile platform_domain; caller configuration about the recorded source, not an observation, an in-band claim or an authenticated vehicle identity",
        ),
        None => (
            if profile.family == musubi_reference_types::Family::Ugv {
                "Ground"
            } else {
                "Unknown"
            }
            .to_string(),
            "profile_declared_family",
            "profile-declared UGV; verify publisher metadata for this source, not an in-band identity claim",
        ),
    };
    serde_json::to_writer(
        std::io::stdout().lock(),
        &json!({
            "profile_identity":profile_identity,"input_sha256":format!("sha256:{:x}",Sha256::digest(&bytes)),
            "identity_basis":"loaded bytes and declared revision; not authenticity or approval",
            "observations":observations,"main_rows":report.source_rows,"source_role":profile.source_role.as_str(),
            "equal_time_order":if allow_equal { "source-order-only" } else { "strictly-increasing" },
            "nonfinite_policy":if preserve_nonfinite { "preserve-as-text" } else { "reject" },
            "source_columns":report.source_columns.iter().map(|c| json!({"name":c.name,"unit":c.unit})).collect::<Vec<_>>(),
            "profile_units":profile.field_units,"platform_domain":platform_domain,
            "domain_source":domain_source,"domain_basis":domain_basis,
        }),
    )?;
    Ok(())
}
