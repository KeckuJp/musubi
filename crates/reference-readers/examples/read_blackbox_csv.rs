use musubi_reference_readers::{
    FieldValue, ProfileReader, blackbox::BlackboxCsvReader, profile::parse_profile,
};
use serde_json::{Value, json};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    let allow_equal = args.len() == 4 && args[3] == "--allow-equal-time";
    if args.len() != 3 && !allow_equal {
        return Err(
            "usage: read_blackbox_csv PROFILE.toml DECODED.csv [--allow-equal-time]".into(),
        );
    }
    let profile = parse_profile(&std::fs::read_to_string(&args[1])?, "development")
        .map_err(|e| format!("profile: {e:?}"))?;
    let bytes = std::fs::read(&args[2])?;
    let table = musubi_decoded_csv::parse_with_options(
        &bytes,
        &profile.fields.time,
        musubi_decoded_csv::ParseOptions {
            allow_equal_time: allow_equal,
            ..Default::default()
        },
    )
    .map_err(|e| format!("CSV: {e:?}"))?;
    let obs = if allow_equal {
        BlackboxCsvReader.read_allow_equal_time(&profile, &bytes)
    } else {
        BlackboxCsvReader.read(&profile, &bytes)
    }
    .map_err(|e| format!("input: {e:?}"))?;
    let observations: Vec<_> = obs.iter().map(|o| {
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
    let mut result = json!({
        "observations":observations,"main_rows":table.rows.len(),
        "source_columns":table.columns.iter().map(|c| json!({"name":c.name,"unit":c.unit})).collect::<Vec<_>>(),
        "profile_units":profile.field_units,"platform_domain":"Unknown",
        "domain_basis":"not inferred from FPV profile or firmware alone",
    });
    if allow_equal {
        result["equal_time_order"] = json!("source-order-only");
    }
    serde_json::to_writer(std::io::stdout().lock(), &result)?;
    Ok(())
}
