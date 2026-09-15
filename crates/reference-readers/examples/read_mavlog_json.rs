use musubi_reference_readers::{
    FieldValue, mavlog_json::MavlogJsonReader, profile::parse_profile_with_identity,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 3 {
        return Err("usage: read_mavlog_json PROFILE.toml EXPORT.jsonl".into());
    }
    let (profile, profile_identity) =
        parse_profile_with_identity(&std::fs::read_to_string(&args[1])?, "development")
            .map_err(|e| format!("profile: {e:?}"))?;
    let bytes = std::fs::read(&args[2])?;
    let report = MavlogJsonReader
        .read_report(&profile, &bytes)
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
            "wall_ms":o.wall_ms,"channel":o.channel.as_str(),"fields":fields})
    }).collect();
    serde_json::to_writer(
        std::io::stdout().lock(),
        &json!({
            "profile_identity":profile_identity,"input_sha256":format!("sha256:{:x}",Sha256::digest(&bytes)),
            "identity_basis":"loaded bytes and declared revision; not authenticity or approval",
            "platform_domain":format!("{:?}", report.platform_domain),
            "firmware_identity":report.firmware_identity,
            "decoded_observations":observations.len(),"untimed_records":report.untimed_records,
            "observations":observations,"source_records":report.records,
            "value_basis":"pymavlink_decoded_no_second_wire_scaling",
            "profile_units":profile.field_units,
        }),
    )?;
    Ok(())
}
