#![cfg_attr(test, allow(clippy::expect_used))]

use std::process::ExitCode;

use musubi_cable_ml_eval::{EvaluationConfig, evaluate_json};
use musubi_cable_sim::SensorTier;

fn main() -> ExitCode {
    match parse_cli(std::env::args().skip(1))
        .and_then(|config| evaluate_json(&config).map_err(|error| error.to_string()))
    {
        Ok(report) => {
            println!("{report}");
            ExitCode::SUCCESS
        }
        Err(reason) => {
            eprintln!("musubi-cable-ml-eval: {reason}");
            ExitCode::from(2)
        }
    }
}

fn parse_cli(arguments: impl IntoIterator<Item = String>) -> Result<EvaluationConfig, String> {
    let mut config = EvaluationConfig::default();
    let mut arguments = arguments.into_iter();
    while let Some(argument) = arguments.next() {
        let value = match argument.as_str() {
            "--split-seed" | "--train-count" | "--test-count" | "--tier" => arguments
                .next()
                .ok_or_else(|| format!("missing value after {argument}"))?,
            "--help" | "-h" => return Err(usage()),
            unknown => return Err(format!("unknown argument {unknown}\n{}", usage())),
        };
        match argument.as_str() {
            "--split-seed" => {
                config.split_seed = value
                    .parse()
                    .map_err(|_| format!("invalid split seed: {value}"))?;
            }
            "--train-count" => {
                config.train_seed_count = value
                    .parse()
                    .map_err(|_| format!("invalid train count: {value}"))?;
            }
            "--test-count" => {
                config.test_seed_count = value
                    .parse()
                    .map_err(|_| format!("invalid test count: {value}"))?;
            }
            "--tier" => {
                config.sensor_tier = SensorTier::parse(&value)
                    .ok_or_else(|| format!("invalid tier {value}; expected t0,t1,t2"))?;
            }
            _ => unreachable!(),
        }
    }
    Ok(config)
}

fn usage() -> String {
    "usage: musubi-cable-ml-eval [--split-seed <u64>] [--train-count <usize>] [--test-count <usize>] [--tier <t0|t1|t2>]".to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_shared_split_configuration() {
        let config = parse_cli(
            [
                "--split-seed",
                "9",
                "--train-count",
                "5",
                "--test-count",
                "3",
                "--tier",
                "t1",
            ]
            .into_iter()
            .map(str::to_owned),
        )
        .expect("valid CLI");
        assert_eq!(config.split_seed, 9);
        assert_eq!(config.train_seed_count, 5);
        assert_eq!(config.test_seed_count, 3);
        assert_eq!(config.sensor_tier, SensorTier::T1);
    }
}
