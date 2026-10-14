use serde::Serialize;

pub const CLOCK_UNTRUSTED_TICK: u32 = 1;
pub const CALIBRATION_UNKNOWN_TICK: u32 = 2;
pub const CALIBRATION_MISSING_TICK: u32 = 3;
pub const EVENT_START_TICK: u32 = 4;
pub const TICK_INTERVAL_NS: i64 = 1_000_000_000;
pub const START_TIME_NS: i64 = 1_788_739_200_000_000_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ScenarioKind {
    NormalDrag,
    PartialSnag,
    CompleteRestraintOrLoop,
    ReelJam,
    LengthLimit,
    TetherBreak,
    MobilityStall,
    ObserverFault,
}

impl ScenarioKind {
    pub const ALL: [Self; 8] = [
        Self::NormalDrag,
        Self::PartialSnag,
        Self::CompleteRestraintOrLoop,
        Self::ReelJam,
        Self::LengthLimit,
        Self::TetherBreak,
        Self::MobilityStall,
        Self::ObserverFault,
    ];

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NormalDrag => "normal_drag",
            Self::PartialSnag => "partial_snag",
            Self::CompleteRestraintOrLoop => "complete_restraint_or_loop",
            Self::ReelJam => "reel_jam",
            Self::LengthLimit => "length_limit",
            Self::TetherBreak => "tether_break",
            Self::MobilityStall => "mobility_stall",
            Self::ObserverFault => "observer_fault",
        }
    }

    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|scenario| scenario.as_str() == value)
    }

    #[must_use]
    pub const fn annotation_label(self) -> &'static str {
        match self {
            Self::NormalDrag => "confirmed_normal_drag",
            Self::PartialSnag => "confirmed_partial_snag",
            Self::CompleteRestraintOrLoop => "confirmed_complete_restraint_or_loop",
            Self::ReelJam => "confirmed_reel_jam",
            Self::LengthLimit => "confirmed_length_limit",
            Self::TetherBreak => "confirmed_tether_break",
            Self::MobilityStall => "confirmed_mobility_stall",
            Self::ObserverFault => "confirmed_observer_fault",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub enum SensorTier {
    #[serde(rename = "t0")]
    T0,
    #[serde(rename = "t1")]
    T1,
    #[serde(rename = "t2")]
    T2,
}

impl SensorTier {
    pub const ALL: [Self; 3] = [Self::T0, Self::T1, Self::T2];

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::T0 => "t0",
            Self::T1 => "t1",
            Self::T2 => "t2",
        }
    }

    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|tier| tier.as_str() == value)
    }

    #[must_use]
    pub const fn has_scalar_tether_sensors(self) -> bool {
        matches!(self, Self::T1 | Self::T2)
    }

    #[must_use]
    pub const fn has_vector_and_geometry_sensors(self) -> bool {
        matches!(self, Self::T2)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SimulationConfig {
    pub scenario: ScenarioKind,
    pub sensor_tier: SensorTier,
    pub seed: u64,
    pub tick_count: u32,
}

impl SimulationConfig {
    #[must_use]
    pub const fn new(scenario: ScenarioKind, sensor_tier: SensorTier, seed: u64) -> Self {
        Self {
            scenario,
            sensor_tier,
            seed,
            tick_count: 10,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Sample {
    pub tick: u32,
    pub event_time_ns: i64,
    pub position_x_m: f64,
    pub motion_reference_mps: f64,
    pub ground_speed_mps: f64,
    pub motor_current_a: f64,
    pub motor_torque_nm: f64,
    pub motor_rpm: f64,
    pub imu_longitudinal_accel_mps2: f64,
    pub link_present: bool,
    pub continuity_present: bool,
    pub payout_length_m: f64,
    pub payout_rate_mps: f64,
    pub reel_load_a: f64,
    pub tension_scalar_n: f64,
    pub vehicle_tension_x_n: f64,
    pub vehicle_tension_y_n: f64,
    pub vehicle_tension_z_n: f64,
    pub reel_tension_x_n: f64,
    pub reel_tension_y_n: f64,
    pub reel_tension_z_n: f64,
    pub exit_pitch_deg: f64,
    pub exit_yaw_deg: f64,
    pub shape_proxy_lateral_offset_m: f64,
    pub vehicle_heading_deg: f64,
    pub observer_liveness: bool,
}

#[derive(Debug, Clone, Copy)]
pub struct Features {
    pub progress_deficit: f64,
    pub effort_to_progress: f64,
    pub payout_motion_residual: f64,
    pub tension_model_residual: f64,
    pub force_transmission_ratio: f64,
    pub tether_angle_misalignment: f64,
    pub continuity_fault: bool,
    pub observer_health: f64,
}

pub fn generate_samples(config: &SimulationConfig, nominal_length_m: f64) -> Vec<Sample> {
    let mut position_x_m = 0.0;
    let mut payout_length_m = nominal_length_m.min(5.0);
    let mut previous_speed = 0.0;
    let mut samples = Vec::with_capacity(config.tick_count as usize);

    for tick in 0..config.tick_count {
        let active = tick >= EVENT_START_TICK;
        let motion_reference_mps = 1.0 + noise(config.seed, tick, 0) * 0.01;
        let normal_speed = 0.86 + noise(config.seed, tick, 1) * 0.015;

        let (ground_speed_mps, motor_current_a, motor_rpm) = match config.scenario {
            ScenarioKind::PartialSnag if active => (0.28, 8.8, 510.0),
            ScenarioKind::CompleteRestraintOrLoop if active => (0.015, 11.5, 85.0),
            ScenarioKind::ReelJam if active => (0.44, 7.5, 690.0),
            ScenarioKind::LengthLimit if active => (0.08, 10.2, 190.0),
            ScenarioKind::TetherBreak if active => (0.94, 3.8, 1_260.0),
            ScenarioKind::MobilityStall if active => (0.01, 13.0, 35.0),
            _ => (normal_speed, 4.2, 1_180.0),
        };
        let ground_speed_mps = ground_speed_mps + noise(config.seed, tick, 2) * 0.004;
        let motor_current_a = motor_current_a + noise(config.seed, tick, 3) * 0.04;
        let motor_rpm = motor_rpm + noise(config.seed, tick, 4) * 2.0;
        // This is a deterministic observation channel, never a motor or motion command.
        let motor_torque_nm =
            (motor_current_a * 0.62 + noise(config.seed, tick, 10) * 0.015).max(0.0);

        let (payout_rate_mps, reel_load_a, tension_n, angle_offset_deg, transmission) = if !active
            || matches!(
                config.scenario,
                ScenarioKind::NormalDrag | ScenarioKind::ObserverFault
            ) {
            (ground_speed_mps * 0.96, 2.0, 8.0, 2.0, 0.98)
        } else {
            match config.scenario {
                ScenarioKind::PartialSnag => {
                    let stick_slip = if tick % 2 == 0 { 3.0 } else { -2.0 };
                    (0.08, 5.5, 20.0 + stick_slip, 28.0, 0.72)
                }
                ScenarioKind::CompleteRestraintOrLoop => (0.0, 8.5, 34.0, 78.0, 0.58),
                ScenarioKind::ReelJam => (0.0, 15.0, 26.0, 3.0, 1.18),
                ScenarioKind::LengthLimit => (0.0, 10.5, 31.0, 4.0, 0.96),
                ScenarioKind::TetherBreak => (0.0, 0.5, 0.2, 8.0, 0.10),
                ScenarioKind::MobilityStall => (0.0, 1.5, 8.0, 2.0, 0.98),
                ScenarioKind::NormalDrag | ScenarioKind::ObserverFault => unreachable!(),
            }
        };

        let payout_rate_mps = payout_rate_mps + noise(config.seed, tick, 5) * 0.002;
        let reel_load_a = reel_load_a + noise(config.seed, tick, 6) * 0.02;
        let tension_n = (tension_n + noise(config.seed, tick, 7) * 0.08).max(0.0);
        let angle_offset_deg = angle_offset_deg + noise(config.seed, tick, 8) * 0.15;

        position_x_m += ground_speed_mps.max(0.0);
        payout_length_m = if active && matches!(config.scenario, ScenarioKind::LengthLimit) {
            nominal_length_m
        } else {
            (payout_length_m + payout_rate_mps.max(0.0)).min(nominal_length_m)
        };

        let observer_liveness = !(active && matches!(config.scenario, ScenarioKind::ObserverFault));
        let continuity_present = !(active && matches!(config.scenario, ScenarioKind::TetherBreak));
        let link_present = continuity_present;
        let vehicle_heading_deg = 15.0 + noise(config.seed, tick, 9) * 0.1;
        let angle_rad = angle_offset_deg.to_radians();
        let vehicle_tension_x_n = tension_n * libm::cos(angle_rad);
        let vehicle_tension_y_n = tension_n * libm::sin(angle_rad);
        let reel_tension_n = tension_n * transmission;
        let reel_angle_rad = (angle_offset_deg * 0.35).to_radians();
        let shape_proxy_lateral_offset_m =
            payout_length_m * libm::sin(angle_rad) * 0.10 + noise(config.seed, tick, 11) * 0.002;

        samples.push(Sample {
            tick,
            event_time_ns: START_TIME_NS + i64::from(tick) * TICK_INTERVAL_NS,
            position_x_m,
            motion_reference_mps,
            ground_speed_mps,
            motor_current_a,
            motor_torque_nm,
            motor_rpm,
            imu_longitudinal_accel_mps2: ground_speed_mps - previous_speed,
            link_present,
            continuity_present,
            payout_length_m,
            payout_rate_mps,
            reel_load_a,
            tension_scalar_n: tension_n,
            vehicle_tension_x_n,
            vehicle_tension_y_n,
            vehicle_tension_z_n: 0.0,
            reel_tension_x_n: reel_tension_n * libm::cos(reel_angle_rad),
            reel_tension_y_n: reel_tension_n * libm::sin(reel_angle_rad),
            reel_tension_z_n: 0.0,
            exit_pitch_deg: 1.0,
            exit_yaw_deg: vehicle_heading_deg + angle_offset_deg,
            shape_proxy_lateral_offset_m,
            vehicle_heading_deg,
            observer_liveness,
        });
        previous_speed = ground_speed_mps;
    }

    samples
}

pub fn derive_features(sample: Sample, normal_tension_n: f64) -> Features {
    let vehicle_tension_n = libm::hypot(
        libm::hypot(sample.vehicle_tension_x_n, sample.vehicle_tension_y_n),
        sample.vehicle_tension_z_n,
    );
    let reel_tension_n = libm::hypot(
        libm::hypot(sample.reel_tension_x_n, sample.reel_tension_y_n),
        sample.reel_tension_z_n,
    );
    let progress_deficit = ((sample.motion_reference_mps - sample.ground_speed_mps)
        / sample.motion_reference_mps.abs().max(0.05))
    .clamp(0.0, 1.0);
    let effort_to_progress = sample.motor_torque_nm / sample.ground_speed_mps.abs().max(0.05);
    let payout_motion_residual = (sample.ground_speed_mps.max(0.0) - sample.payout_rate_mps).abs()
        / sample.motion_reference_mps.abs().max(0.05);
    let tension_model_residual =
        (sample.tension_scalar_n - normal_tension_n).abs() / normal_tension_n.abs().max(0.1);
    let force_transmission_ratio = reel_tension_n / vehicle_tension_n.max(0.1);
    let tether_angle_misalignment =
        wrapped_angle_difference_deg(sample.exit_yaw_deg, sample.vehicle_heading_deg);

    Features {
        progress_deficit,
        effort_to_progress,
        payout_motion_residual,
        tension_model_residual,
        force_transmission_ratio,
        tether_angle_misalignment,
        continuity_fault: !sample.continuity_present,
        observer_health: if sample.observer_liveness { 1.0 } else { 0.0 },
    }
}

fn wrapped_angle_difference_deg(left: f64, right: f64) -> f64 {
    let mut difference = (left - right).rem_euclid(360.0);
    if difference > 180.0 {
        difference = 360.0 - difference;
    }
    difference
}

fn noise(seed: u64, tick: u32, channel: u64) -> f64 {
    let mixed = splitmix64(seed ^ u64::from(tick).wrapping_mul(0x9e37_79b9_7f4a_7c15) ^ channel);
    let mantissa = mixed >> 11;
    let unit = mantissa as f64 / ((1_u64 << 53) - 1) as f64;
    unit * 2.0 - 1.0
}

const fn splitmix64(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scenario_and_tier_labels_round_trip() {
        for scenario in ScenarioKind::ALL {
            assert_eq!(ScenarioKind::parse(scenario.as_str()), Some(scenario));
        }
        for tier in SensorTier::ALL {
            assert_eq!(SensorTier::parse(tier.as_str()), Some(tier));
        }
    }

    #[test]
    fn fixture_states_have_distinct_direct_cues() {
        let at_event = |scenario| {
            generate_samples(&SimulationConfig::new(scenario, SensorTier::T2, 7), 50.0)[7]
        };
        let snag = at_event(ScenarioKind::PartialSnag);
        let broken = at_event(ScenarioKind::TetherBreak);
        let stalled = at_event(ScenarioKind::MobilityStall);

        assert!(snag.continuity_present);
        assert!(snag.tension_scalar_n > 15.0);
        assert!(!broken.continuity_present);
        assert!(broken.tension_scalar_n < 1.0);
        assert!(stalled.continuity_present);
        assert!(stalled.motor_torque_nm > 7.0);
        assert!(stalled.tension_scalar_n < 10.0);
        assert!(snag.shape_proxy_lateral_offset_m > stalled.shape_proxy_lateral_offset_m);
    }
}
