#![cfg_attr(test, allow(clippy::expect_used))]

use std::path::PathBuf;
use std::process::ExitCode;

use musubi_cable_sim::{
    ScenarioKind, SensorTier, SimulationConfig, generate_ndjson, write_create_new,
};

#[derive(Debug)]
struct Cli {
    output: PathBuf,
    config: SimulationConfig,
}

fn main() -> ExitCode {
    match parse_cli(std::env::args().skip(1)).and_then(|cli| run(&cli)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(reason) => {
            eprintln!("musubi-cable-sim: {reason}");
            ExitCode::from(2)
        }
    }
}

fn run(cli: &Cli) -> Result<(), String> {
    let artifact = generate_ndjson(&cli.config).map_err(|error| error.to_string())?;
    write_create_new(&cli.output, &artifact).map_err(|error| error.to_string())?;
    eprintln!(
        "created {} ({}; {})",
        cli.output.display(),
        artifact.pre_receipt_sha256,
        musubi_cable_sim::WATERMARK
    );
    Ok(())
}

fn parse_cli(arguments: impl IntoIterator<Item = String>) -> Result<Cli, String> {
    let mut scenario = None;
    let mut tier = None;
    let mut seed = None;
    let mut output = None;
    let mut tick_count = 10_u32;
    let mut arguments = arguments.into_iter();

    while let Some(argument) = arguments.next() {
        let value = match argument.as_str() {
            "--scenario" | "--tier" | "--seed" | "--out" | "--ticks" => arguments
                .next()
                .ok_or_else(|| format!("missing value after {argument}"))?,
            "--help" | "-h" => return Err(usage()),
            unknown => return Err(format!("unknown argument {unknown}\n{}", usage())),
        };

        match argument.as_str() {
            "--scenario" => {
                scenario = Some(ScenarioKind::parse(&value).ok_or_else(|| {
                    format!(
                        "unknown scenario {value}; expected {}",
                        ScenarioKind::ALL
                            .into_iter()
                            .map(ScenarioKind::as_str)
                            .collect::<Vec<_>>()
                            .join(",")
                    )
                })?);
            }
            "--tier" => {
                tier = Some(
                    SensorTier::parse(&value)
                        .ok_or_else(|| format!("unknown tier {value}; expected t0,t1,t2"))?,
                );
            }
            "--seed" => {
                seed = Some(
                    value
                        .parse::<u64>()
                        .map_err(|_| format!("seed must be an unsigned integer: {value}"))?,
                );
            }
            "--out" => output = Some(PathBuf::from(value)),
            "--ticks" => {
                tick_count = value
                    .parse::<u32>()
                    .map_err(|_| format!("ticks must be an unsigned integer: {value}"))?;
            }
            _ => unreachable!(),
        }
    }

    let scenario = scenario.ok_or_else(|| format!("--scenario is required\n{}", usage()))?;
    let sensor_tier = tier.ok_or_else(|| format!("--tier is required\n{}", usage()))?;
    let seed = seed.ok_or_else(|| format!("--seed is required\n{}", usage()))?;
    let output = output.ok_or_else(|| format!("--out is required\n{}", usage()))?;
    Ok(Cli {
        output,
        config: SimulationConfig {
            scenario,
            sensor_tier,
            seed,
            tick_count,
        },
    })
}

fn usage() -> String {
    concat!(
        "usage: musubi-cable-sim ",
        "--scenario <normal_drag|partial_snag|complete_restraint_or_loop|reel_jam|length_limit|tether_break|mobility_stall|observer_fault> ",
        "--tier <t0|t1|t2> --seed <u64> --out <new-file.ndjson> ",
        "[--ticks <u32>]"
    )
    .to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_explicit_reproducibility_inputs() {
        let cli = parse_cli(
            [
                "--scenario",
                "partial_snag",
                "--tier",
                "t2",
                "--seed",
                "42",
                "--out",
                "fixture.ndjson",
                "--ticks",
                "12",
            ]
            .into_iter()
            .map(str::to_owned),
        )
        .expect("valid CLI");
        assert_eq!(cli.config.scenario, ScenarioKind::PartialSnag);
        assert_eq!(cli.config.sensor_tier, SensorTier::T2);
        assert_eq!(cli.config.seed, 42);
        assert_eq!(cli.config.tick_count, 12);
    }

    #[test]
    fn rejects_free_form_public_contract_versions() {
        let error = parse_cli(
            [
                "--scenario",
                "partial_snag",
                "--tier",
                "t2",
                "--seed",
                "42",
                "--out",
                "fixture.ndjson",
                "--profile-version",
                "profile-7",
            ]
            .into_iter()
            .map(str::to_owned),
        )
        .expect_err("embedded public contracts cannot be overridden");
        assert!(error.contains("unknown argument --profile-version"));
    }
}
