use musubi_reference_readers::{
    FieldValue,
    mseed_waveform::{MseedWaveformReader, field_units},
};
use serde_json::{Value, json};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    let with_samples = args.len() == 3 && args[2] == "--samples";
    if args.len() != 2 && !with_samples {
        return Err("usage: read_mseed INPUT.mseed [--samples]".into());
    }
    let bytes = std::fs::read(&args[1])?;
    let report = MseedWaveformReader
        .read_report(&bytes)
        .map_err(|e| format!("input: {e}"))?;

    let observations: Vec<Value> = report
        .observations
        .iter()
        .map(|o| {
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
                   "channel":o.channel.as_str(),"source_role":o.source_role.as_str(),"stale":o.stale,
                   "fields":fields})
        })
        .collect();

    let records: Vec<Value> = report
        .accounting
        .iter()
        .zip(&report.records)
        .map(|(a, r)| {
            json!({"record_index":a.record_index,"record_offset":a.record_offset,"fdsn_source_id":a.source_id,
                   "record_sequence":a.sequence,"data_quality_indicator":r.quality.to_string(),
                   "record_length_bytes":a.record_length_bytes,"data_begin_offset":a.data_begin_offset,
                   "sample_count_declared":a.sample_count_declared,"sample_count_decoded":a.sample_count_decoded,
                   "record_start_seed":r.start.to_seed_string(),"record_start_unix_us":r.start_unix_us,
                   "record_start_unix_us_raw":r.start_unix_us_raw,"time_correction_ticks":r.time_correction_ticks,
                   "time_correction_applied_flag":i64::from(r.time_correction_applied),
                   "blockette1001_microsecond":r.microsecond_offset,"timing_quality_percent":r.timing_quality_percent,
                   "sample_rate_factor":r.rate_factor,"sample_rate_multiplier":r.rate_multiplier,
                   "sample_rate_hz":r.sample_rate_hz(),"sample_period_us":r.sample_period_us,
                   "encoding_code":r.encoding,"encoding_name":r.encoding_name(),"word_order":r.word_order,
                   "activity_flags":r.activity_flags,"io_clock_flags":r.io_clock_flags,
                   "data_quality_flags":r.data_quality_flags,"clock_locked_flag":i64::from(r.clock_locked_flag()),
                   "blockette_types":a.blockette_types,"blockette_bytes":a.blockette_bytes,
                   "reserved_gap_bytes":a.reserved_gap_bytes,"bytes_balanced":a.bytes_balanced(),
                   "unknown_blockette_types":a.unknown_blockette_types,
                   "unknown_blockette_bytes":a.unknown_blockette_bytes,
                   "steim_x0":r.steim_x0,"steim_xn":r.steim_xn,"steim_surplus_diffs":a.steim_surplus_diffs,
                   "gap_to_previous_us":a.gap_to_previous_us,"stale":a.stale,
                   "samples":if with_samples { json!(r.samples) } else { Value::Null }})
        })
        .collect();

    let unknown: Vec<Value> = report
        .unknown_blockettes
        .iter()
        .map(|b| {
            let hex: String = b.raw.iter().map(|byte| format!("{byte:02x}")).collect();
            json!({"record_index":b.record_index,"record_offset":b.record_offset,"offset":b.offset,
                   "kind":b.kind,"bytes":b.raw.len(),"raw_hex":hex})
        })
        .collect();

    serde_json::to_writer(
        std::io::stdout().lock(),
        &json!({
            "observations":observations,
            "observation_count":report.observations.len(),
            "record_count":report.records.len(),
            "total_samples_decoded":report.total_samples_decoded,
            "streams":report.streams,
            "records":records,
            "unknown_blockettes":unknown,
            "unknown_blockette_types":report.unknown_blockette_types,
            "unknown_blockette_bytes":report.unknown_blockette_bytes,
            "units":field_units(),
            "platform_domain":"Unknown",
            "domain_basis":"not-asserted-by-record-bytes",
            "clock_note":"station-declared start time with SEED header correction and blockette 1001 microseconds applied; synchronisation unverified",
            "value_note":"instrument counts, no instrument response applied",
        }),
    )?;
    Ok(())
}
