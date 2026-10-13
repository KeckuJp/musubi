use musubi_reference_readers::{
    FieldValue, Observation, ProfileReader, profile::parse_profile_with_identity, recorded_failure,
    telemetry_csv::TelemetryCsvReader,
};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io::Read;

// Private input-document shape, not a new shared/core type or schema engine.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProtocolExpectation {
    input_sha256: String,
    profile_sha256: String,
    expected_wire_version: u64,
    session_id: String,
    citation: String,
}

fn bounded(path: &str, limit: usize) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let file = std::fs::File::open(path)?;
    if !file.metadata()?.is_file() {
        return Err("saved regular file required".into());
    }
    let mut bytes = Vec::new();
    file.take(limit as u64 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err("saved input bound exceeded".into());
    }
    Ok(bytes)
}

fn observation_json(o: &Observation) -> Value {
    let fields: serde_json::Map<String, Value> = o
        .fields
        .iter()
        .map(|(key, value)| {
            let v = match value {
                FieldValue::I64(n) => json!(n),
                FieldValue::F64(n) => json!(n),
                FieldValue::Text(s) => json!(s),
                FieldValue::Blank => Value::Null,
            };
            (key.clone(), v)
        })
        .collect();
    json!({"t_ms":o.t_ms,"t_boot_us":o.t_boot_us,"clock_basis":format!("{:?}",o.clock_basis),
        "wall_ms":o.wall_ms,"anchor_unix_us":o.anchor_unix_us,"time_confidence":o.time_confidence,
        "channel":o.channel.as_str(),"fields":fields})
}

fn declared_domain(
    profile: &musubi_reference_types::FamilyProfile,
) -> (String, &'static str, &'static str) {
    match profile.declared_platform_domain {
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
    }
}

fn protocol_read(
    profile_path: &str,
    input: &str,
    expectation_path: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    use musubi_reference_readers::{ClockBasis, SourceRole, field, tlog::TlogReader};
    let pbytes = bounded(profile_path, 64 * 1024)?;
    let bytes = bounded(input, 16 * 1024 * 1024)?;
    let ebytes = bounded(expectation_path, 4096)?;
    let expected: ProtocolExpectation = serde_json::from_slice(&ebytes)?;
    let digest = |b: &[u8]| format!("sha256:{:x}", Sha256::digest(b));
    let label = |s: &str| !s.trim().is_empty() && s.chars().count() <= 256;
    if !matches!(expected.expected_wire_version, 1 | 2)
        || expected.input_sha256 != digest(&bytes)
        || expected.profile_sha256 != digest(&pbytes)
        || !label(&expected.session_id)
        || !label(&expected.citation)
    {
        return Err("invalid or unbound protocol expectation".into());
    }
    let (profile, identity) =
        parse_profile_with_identity(std::str::from_utf8(&pbytes)?, "development")
            .map_err(|e| format!("profile: {e:?}"))?;
    if profile.format != "mavlink_tlog"
        || profile.fields.time != "host_recorded_us"
        || profile.source_role != SourceRole::Gcs
        || profile.default_clock_basis != ClockBasis::Unknown
        || profile.declared_sender.is_none()
    {
        return Err(
            "protocol comparison requires selected GCS sender and Unknown host_recorded_us".into(),
        );
    }
    let rows = TlogReader
        .read(&profile, &bytes)
        .map_err(|e| format!("input: {e:?}"))?;
    if rows.is_empty() || rows.len() > 20000 {
        return Err("protocol selected observation count bound".into());
    }
    let count = |key| match field(&rows[0], key) {
        Some(FieldValue::I64(n)) if *n >= 0 => Ok(*n),
        _ => Err("missing selected sender accounting"),
    };
    let selected = count("MAVLINK.selected_sender_records")?;
    let excluded = count("MAVLINK.unselected_sender_records")?;
    if selected as usize != rows.len() {
        return Err("inconsistent selected sender count".into());
    }
    let catalog = recorded_failure::protocol_catalog()?;
    let (platform_domain, domain_source, domain_basis) = declared_domain(&profile);
    // Keep legacy CSV bytes unchanged, but do not copy its UGV-only prose to
    // a new native report whose family establishes no domain.
    let domain_basis = if profile.declared_platform_domain.is_none()
        && profile.family != musubi_reference_types::Family::Ugv
    {
        "profile family establishes no platform domain; Unknown, not a sensed or authenticated identity"
    } else {
        domain_basis
    };
    let output = json!({
        "profile_identity":identity, "input_sha256":digest(&bytes),
        "expectation_sha256":digest(&ebytes), "session_id":expected.session_id,
        "citation":expected.citation, "scope":"caller_declared_file_sender_session_not_authenticated",
        "main_rows":rows.len(), "selected_sender_records":selected, "unselected_sender_records":excluded,
        "source_role":"gcs", "profile_units":profile.field_units,
        "platform_domain":platform_domain, "domain_source":domain_source, "domain_basis":domain_basis,
        "observations":rows.iter().map(observation_json).collect::<Vec<_>>(),
        "recorded_failure_candidates":rows.iter().map(|o| recorded_failure::protocol_as_json(o, &catalog, expected.expected_wire_version)).collect::<Vec<_>>(),
    });
    // In-memory cap after serialization, before any stdout; not a streaming memory bound.
    let rendered = serde_json::to_vec(&output)?;
    if rendered.len() > 64 * 1024 * 1024 {
        return Err("protocol output bound exceeded".into());
    }
    std::io::Write::write_all(&mut std::io::stdout().lock(), &rendered)?;
    Ok(())
}

fn refusal_destination(target: &str, sources: [&str; 2]) -> Result<(), Box<dyn std::error::Error>> {
    use std::path::Path;
    let normalized = |name: &str| -> std::io::Result<std::path::PathBuf> {
        let path = Path::new(name);
        if path.exists() {
            return path.canonicalize();
        }
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        Ok(parent.canonicalize()?.join(path.file_name().ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "filename required")
        })?))
    };
    if Path::new(target).try_exists()? || std::fs::symlink_metadata(target).is_ok() {
        return Err("fresh refusal sidecar required".into());
    }
    let target = normalized(target)?;
    for source in sources {
        if normalized(source).is_ok_and(|source| source == target) {
            return Err("refusal sidecar must not alias input or profile".into());
        }
    }
    Ok(())
}

fn refusal_bytes(
    path: &str,
    limit: usize,
    evidence: &mut Value,
) -> Result<Vec<u8>, recorded_failure::SavedReadRefusal> {
    use recorded_failure::SavedReadRefusal as Failure;
    evidence["read_status"] = json!("ATTEMPTED_NOT_COMPLETE");
    evidence["operation"] = json!("metadata");
    let metadata = std::fs::metadata(path).map_err(Failure::Io)?;
    if !metadata.is_file() {
        return Err(Failure::NotRegular);
    }
    if metadata.len() > limit as u64 {
        evidence["read_status"] = json!("OVER_BOUND_NOT_READ");
        return Err(Failure::BoundExceeded { limit });
    }
    evidence["operation"] = json!("open");
    let file = std::fs::File::open(path).map_err(Failure::Io)?;
    evidence["operation"] = json!("opened_file_metadata");
    if !file.metadata().map_err(Failure::Io)?.is_file() {
        return Err(Failure::NotRegular);
    }
    let mut bytes = Vec::new();
    evidence["operation"] = json!("bounded_read_to_end");
    let result = file.take(limit as u64 + 1).read_to_end(&mut bytes);
    evidence["bytes_read"] = json!(bytes.len());
    result.map_err(Failure::Io)?;
    if bytes.len() > limit {
        evidence["read_status"] = json!("OVER_BOUND_PREFIX_ONLY");
        return Err(Failure::BoundExceeded { limit });
    }
    evidence["read_status"] = json!("COMPLETE_LOADED_BYTES");
    evidence["sha256"] = json!(format!("sha256:{:x}", Sha256::digest(&bytes)));
    Ok(bytes)
}

fn read_with_refusal(
    profile_path: &str,
    input_path: &str,
    destination: &str,
    options: musubi_decoded_csv::ParseOptions,
    expected_bytes: Option<(usize, &str)>,
) -> Result<
    (
        musubi_reference_types::FamilyProfile,
        musubi_reference_readers::profile::ProfileSourceIdentity,
        Vec<u8>,
        musubi_reference_readers::telemetry_csv::ReadReport,
    ),
    Box<dyn std::error::Error>,
> {
    use recorded_failure::SavedReadRefusal as Failure;
    refusal_destination(destination, [profile_path, input_path])?;
    let unread = |path: &str| {
        json!({"path":path,"sha256":Value::Null,"operation":Value::Null,
        "bytes_read":0,"read_status":"NOT_ATTEMPTED"})
    };
    let mut profile_evidence = unread(profile_path);
    let mut input_evidence = unread(input_path);
    if let Some((size, sha)) = expected_bytes {
        input_evidence["expected_bytes"] = json!({"size":size,"sha256":sha,
            "basis":"caller_declared_not_authenticated", "comparison":"NOT_PERFORMED"});
    }
    let mut profile_declaration = Value::Null;
    let mut stage = "profile_read";
    let attempt = (|| -> Result<_, Failure> {
        let pbytes = refusal_bytes(profile_path, 64 * 1024, &mut profile_evidence)?;
        stage = "profile_decode";
        let source = std::str::from_utf8(&pbytes).map_err(|_| Failure::ProfileEncoding)?;
        let (profile, identity) =
            parse_profile_with_identity(source, "development").map_err(Failure::Reader)?;
        profile_declaration = json!({"identity":identity,"format":profile.format,
            "clock_basis":format!("{:?}",profile.default_clock_basis),
            "basis":"caller_profile_not_authenticated_producer"});
        stage = "input_read";
        let bytes = refusal_bytes(input_path, 16 * 1024 * 1024, &mut input_evidence)?;
        if let Some((size, sha)) = expected_bytes {
            stage = "input_expectation";
            input_evidence["expected_bytes"]["comparison"] =
                json!("same_loaded_bytes_passed_to_reader");
            if size != bytes.len() || input_evidence["sha256"] != json!(sha) {
                return Err(Failure::ExpectedBytesMismatch);
            }
        }
        stage = "input_decode";
        let report = TelemetryCsvReader
            .read_report_with_options(&profile, &bytes, options)
            .map_err(Failure::Reader)?;
        Ok((profile, identity, bytes, report))
    })();
    match attempt {
        Ok(result) => Ok(result),
        Err(error) => {
            let mut report = recorded_failure::telemetry_refusal_as_json(&error);
            report["stage"] = json!(stage);
            report["reader"] = json!("telemetry_csv_us");
            report["profile"] = profile_evidence;
            report["profile_declaration"] = profile_declaration;
            report["input"] = input_evidence;
            report["options"] = json!({"allow_equal_time":options.allow_equal_time,
                "preserve_nonfinite_as_text":options.preserve_nonfinite_as_text});
            report["source_basis"] =
                json!("loaded_bytes_not_authenticated_producer_or_atomic_live_snapshot");
            let rendered = serde_json::to_vec(&report)?;
            if rendered.len() > 64 * 1024 {
                return Err(
                    "read refused; refusal sidecar exceeds output bound (not saved)".into(),
                );
            }
            let mut output = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(destination)
                .map_err(|e| format!("read refused; refusal sidecar could not be created: {e}"))?;
            std::io::Write::write_all(&mut output, &rendered)
                .map_err(|e| format!("read refused; refusal sidecar write incomplete: {e}"))?;
            Err(format!(
                "read refused at {stage}; typed refusal evidence saved; no observations accepted"
            )
            .into())
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() < 3 {
        return Err(
            "usage: read_telemetry_csv PROFILE.toml CONVERTED.csv [--allow-equal-time] [--preserve-nonfinite-as-text] [--recorded-failure-candidates] [--recorded-failure-context CONTEXT.json]; native opt-in: PROFILE.toml SAVED.tlog --recorded-protocol-expectation EXPECTATION.json".into(),
        );
    }
    let mut allow_equal = false;
    let mut allow_empty = false;
    let mut preserve_nonfinite = false;
    let mut failures = false;
    let mut context_path = None;
    let mut protocol_path = None;
    let mut refusal_path = None;
    let mut expected_size = None;
    let mut expected_sha = None;
    let mut index = 3;
    while index < args.len() {
        match args[index].as_str() {
            "--expected-input-size" if expected_size.is_none() && index + 1 < args.len() => {
                index += 1;
                let value = &args[index];
                if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
                    return Err("expected input size must be decimal bytes".into());
                }
                let size: usize = value.parse()?;
                if size > 16 * 1024 * 1024 {
                    return Err("expected input size exceeds refusal input bound".into());
                }
                expected_size = Some(size);
            }
            "--expected-input-sha256" if expected_sha.is_none() && index + 1 < args.len() => {
                index += 1;
                let value = args[index].as_str();
                let hex = value.strip_prefix("sha256:").unwrap_or("");
                if hex.len() != 64
                    || !hex
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                {
                    return Err("expected SHA must be sha256: plus 64 lowercase hex digits".into());
                }
                expected_sha = Some(value);
            }
            "--read-refusal-report" if refusal_path.is_none() && index + 1 < args.len() => {
                index += 1;
                refusal_path = Some(args[index].as_str());
            }
            "--recorded-protocol-expectation"
                if protocol_path.is_none() && index + 1 < args.len() =>
            {
                index += 1;
                protocol_path = Some(&args[index]);
            }
            "--allow-equal-time" => allow_equal = true,
            "--allow-empty-rows" => allow_empty = true,
            "--preserve-nonfinite-as-text" => preserve_nonfinite = true,
            "--recorded-failure-candidates" => failures = true,
            "--recorded-failure-context" if context_path.is_none() && index + 1 < args.len() => {
                index += 1;
                context_path = Some(&args[index]);
            }
            _ => return Err("unsupported or incomplete reader option".into()),
        }
        index += 1;
    }
    if expected_size.is_some() != expected_sha.is_some()
        || (expected_size.is_some() && refusal_path.is_none())
    {
        return Err("expected size and SHA pair requires read refusal report".into());
    }
    if let Some(path) = protocol_path {
        if allow_equal
            || allow_empty
            || preserve_nonfinite
            || failures
            || context_path.is_some()
            || refusal_path.is_some()
        {
            return Err("protocol expectation cannot mix CSV or storage candidate options".into());
        }
        return protocol_read(&args[1], &args[2], path);
    }
    if refusal_path.is_some() && (failures || context_path.is_some()) {
        return Err("read refusal sidecar cannot mix recorded candidate/context options".into());
    }
    if context_path.is_some() && !failures {
        return Err("recorded failure context requires explicit candidate opt-in".into());
    }
    let context: Option<recorded_failure::ConfigurationComparison> =
        if let Some(path) = context_path {
            let mut bytes = Vec::new();
            std::fs::File::open(path)?
                .take(4097)
                .read_to_end(&mut bytes)?;
            if bytes.len() > 4096 {
                return Err("recorded failure context exceeds bound".into());
            }
            Some(serde_json::from_slice(&bytes)?)
        } else {
            None
        };
    let options = musubi_decoded_csv::ParseOptions {
        allow_empty_rows: allow_empty,
        allow_equal_time: allow_equal,
        preserve_nonfinite_as_text: preserve_nonfinite,
    };
    let (profile, profile_identity, bytes, report) = if let Some(path) = refusal_path {
        read_with_refusal(
            &args[1],
            &args[2],
            path,
            options,
            expected_size.zip(expected_sha),
        )?
    } else {
        let (profile, profile_identity) =
            parse_profile_with_identity(&std::fs::read_to_string(&args[1])?, "development")
                .map_err(|e| format!("profile: {e:?}"))?;
        let bytes = std::fs::read(&args[2])?;
        let report = TelemetryCsvReader
            .read_report_with_options(&profile, &bytes, options)
            .map_err(|e| format!("input: {e:?}"))?;
        (profile, profile_identity, bytes, report)
    };
    let observations: Vec<_> = report.observations.iter().map(observation_json).collect();
    // Declared, never sensed. An explicit `platform_domain` is used when the profile
    // source qualified itself; otherwise the family's established fallback is unchanged.
    let (platform_domain, domain_source, domain_basis) = declared_domain(&profile);
    let mut output = json!({
        "profile_identity":profile_identity,"input_sha256":format!("sha256:{:x}",Sha256::digest(&bytes)),
        "identity_basis":"loaded bytes and declared revision; not authenticity or approval",
        "observations":observations,"main_rows":report.source_rows,"source_role":profile.source_role.as_str(),
        "equal_time_order":if allow_equal { "source-order-only" } else { "strictly-increasing" },
        "nonfinite_policy":if preserve_nonfinite { "preserve-as-text" } else { "reject" },
        "source_columns":report.source_columns.iter().map(|c| json!({"name":c.name,"unit":c.unit})).collect::<Vec<_>>(),
        "profile_units":profile.field_units,"platform_domain":platform_domain,
        "domain_source":domain_source,"domain_basis":domain_basis,
    });
    if failures {
        let catalog = if profile.profile_id == "declared_schema_unit_checks_v1" {
            recorded_failure::comparison_catalog()?
        } else if profile.profile_id == "producer_software_version_expectation_v1" {
            recorded_failure::producer_version_catalog()?
        } else if profile.profile_id == "identifier_uniqueness_v1" {
            recorded_failure::identifier_uniqueness_catalog()?
        } else {
            recorded_failure::catalog()?
        };
        output["recorded_failure_candidates"] = json!(
            report
                .observations
                .iter()
                .map(|o| recorded_failure::as_json(o, &catalog, context.as_ref()))
                .collect::<Vec<_>>()
        );
    }
    serde_json::to_writer(std::io::stdout().lock(), &output)?;
    Ok(())
}
