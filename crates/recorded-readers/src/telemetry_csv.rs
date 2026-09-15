//! Offline telemetry_csv decoding and record preservation. See the recorded-input guide.
use crate::config::{ChannelId, ClockBasis, ReadProfile};
use crate::{Observation, ProfileReader, ReadError};
use musubi_decoded_csv::ParseOptions;

#[derive(Debug, Clone, Copy)]
pub struct TelemetryCsvReader;

#[derive(Debug)]
pub struct ReadReport {
    pub source_columns: Vec<musubi_decoded_csv::Column>,
    pub source_rows: usize,
    pub observations: Vec<Observation>,
}

impl ProfileReader for TelemetryCsvReader {
    fn format_id(&self) -> &'static str {
        "telemetry_csv_us"
    }
    fn read(&self, profile: &ReadProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        self.read_with_options(profile, bytes, ParseOptions::default())
    }
}

impl TelemetryCsvReader {
    pub fn read_non_decreasing(
        &self,
        profile: &ReadProfile,
        bytes: &[u8],
    ) -> Result<Vec<Observation>, ReadError> {
        self.read_with_options(
            profile,
            bytes,
            ParseOptions {
                allow_equal_time: true,
                ..ParseOptions::default()
            },
        )
    }

    pub fn read_with_options(
        &self,
        profile: &ReadProfile,
        bytes: &[u8],
        options: ParseOptions,
    ) -> Result<Vec<Observation>, ReadError> {
        Ok(self
            .read_report_with_options(profile, bytes, options)?
            .observations)
    }

    pub fn read_report_with_options(
        &self,
        profile: &ReadProfile,
        bytes: &[u8],
        options: ParseOptions,
    ) -> Result<ReadReport, ReadError> {
        if profile.format != self.format_id()
            || !matches!(
                profile.default_clock_basis,
                ClockBasis::Unknown | ClockBasis::BootRelative
            )
        {
            return Err(ReadError::Profile(
                "telemetry CSV requires its own profile and unknown/boot_relative clock".into(),
            ));
        }
        let mut csv_profile = profile.clone();
        csv_profile.format = "blackbox_decoded_csv".into();
        let single_channel = if profile.channels.len() == 1 {
            csv_profile.channels = vec![ChannelId::Onboard];
            Some(profile.channels[0])
        } else {
            None
        };
        let table = musubi_decoded_csv::parse_with_options(bytes, &profile.fields.time, options)
            .map_err(|error| ReadError::Malformed {
                offset: error.line,
                what: error.reason.into(),
            })?;
        let mut observations = crate::blackbox::observations_from_table(&csv_profile, &table)?;
        if let Some(channel) = single_channel {
            for o in &mut observations {
                o.channel = channel;
                o.digest = crate::observation_digest(o.t_ms, channel, &o.fields);
            }
        }
        if profile.default_clock_basis == ClockBasis::Unknown {
            for o in &mut observations {
                o.clock_basis = ClockBasis::Unknown;
                o.time_confidence = 0.0;
                o.t_boot_us = None;
                o.wall_ms = None;
                o.anchor_unix_us = None;
            }
        }
        Ok(ReadReport {
            source_columns: table.columns,
            source_rows: table.rows.len(),
            observations,
        })
    }
}
