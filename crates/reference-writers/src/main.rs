#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::too_many_lines,
    clippy::missing_const_for_fn,
    clippy::module_name_repetitions
)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

use std::fs;
use std::path::PathBuf;

use clap::Parser;
use musubi_reference_scenario::{
    ChannelId, FailureKind, Family, FcLogFormat, HandsetLossMode, Injection, LinkProfile,
    LogEndMode, PreludeOrder, RadioStatusVariant, Scenario, generate, pre_demo_default,
};
use musubi_reference_writers::video::{materialize_dvr_dir, segments_for};
use musubi_reference_writers::{FamilyWriter, default_writers, render_all};

#[derive(Parser, Debug)]
#[command(name = "musubi-reference-synth", version, about)]
struct Args {
    #[arg(long, default_value = "predemo_out")]
    out: PathBuf,
    #[arg(long, default_value_t = 42)]
    seed: u64,
    #[arg(long)]
    t0_unix_us: Option<i64>,
    #[arg(long)]
    duration_ms: Option<u64>,
    #[arg(long)]
    no_default_injections: bool,
    #[arg(long = "inject")]
    injections: Vec<String>,
    #[arg(long, default_value = "analog_rf")]
    fpv_link: String,
    #[arg(long, default_value = "px4")]
    plane_fc: String,
    #[arg(long = "drop-channel")]
    drop_channels: Vec<String>,
    #[arg(long)]
    handset_tmr10ms: bool,
    #[arg(long)]
    no_dvr_dir: bool,
}

fn parse_injection(spec: &str) -> Result<Injection, String> {
    let parts: Vec<&str> = spec.split(',').map(str::trim).collect();
    if parts.len() < 4 {
        return Err(format!(
            "inject '{spec}': need KIND,FAMILY,START_MS,DURATION_MS"
        ));
    }
    let kind = match parts[0] {
        "rc_link_loss" => FailureKind::RcLinkLoss,
        "telemetry_link_loss" => FailureKind::TelemetryLinkLoss,
        "fiber_break" => FailureKind::FiberBreak,
        "fc_failure" => FailureKind::FcFailure,
        "camera_stop" => FailureKind::CameraStop,
        "gnss_degradation" => FailureKind::GnssDegradation,
        "gnss_inconsistency" => FailureKind::GnssInconsistency,
        other => return Err(format!("unknown failure kind '{other}'")),
    };
    let family = Family::parse(parts[1]).ok_or_else(|| format!("unknown family '{}'", parts[1]))?;
    if family == Family::Unknown {
        return Err("unknown family is recorded-input only, not a simulation target".into());
    }
    let start: u64 = parts[2]
        .parse()
        .map_err(|_| format!("bad START_MS '{}'", parts[2]))?;
    let dur: u64 = parts[3]
        .parse()
        .map_err(|_| format!("bad DURATION_MS '{}'", parts[3]))?;
    let mut inj = Injection::new(kind, start, dur, family);
    for kv in &parts[4..] {
        let (k, v) = kv
            .split_once('=')
            .ok_or_else(|| format!("bad option '{kv}' (key=value)"))?;
        match (k, v) {
            ("prelude_ms", v) => {
                inj.params.prelude_ms = v.parse().map_err(|_| format!("bad prelude_ms '{v}'"))?;
            }
            ("prelude", "noise") => inj.params.prelude_order = PreludeOrder::NoiseLimited,
            ("prelude", "interference") => {
                inj.params.prelude_order = PreludeOrder::InterferenceLimited;
            }
            ("end", "truncated") => inj.params.log_end_mode = LogEndMode::Truncated,
            ("end", v) if v.starts_with("reboot") => {
                let after_ms = v
                    .split_once(':')
                    .map_or(Ok(20_000), |(_, ms)| ms.parse::<u64>())
                    .map_err(|_| format!("bad reboot '{v}'"))?;
                inj.params.log_end_mode = LogEndMode::Reboot { after_ms };
            }
            ("handset", "zero") => inj.params.handset_loss_mode = HandsetLossMode::ZeroWithGpsBlank,
            ("handset", "frozen") => inj.params.handset_loss_mode = HandsetLossMode::Frozen,
            ("radio", "stops") => inj.params.radio_status = RadioStatusVariant::Stops,
            ("radio", "alive") => inj.params.radio_status = RadioStatusVariant::RemoteAlive,
            _ => return Err(format!("unknown option '{kv}'")),
        }
    }
    Ok(inj)
}

fn build_scenario(args: &Args) -> Result<Scenario, String> {
    let mut s = pre_demo_default(args.seed);
    if let Some(t0) = args.t0_unix_us {
        s.t0_unix_us = t0;
    }
    if let Some(d) = args.duration_ms {
        s.duration_ms = d;
    }
    if args.no_default_injections {
        s.injections.clear();
    }
    for spec in &args.injections {
        s.inject_with(parse_injection(spec)?);
    }
    let fpv_link = LinkProfile::parse(&args.fpv_link)
        .ok_or_else(|| format!("unknown --fpv-link '{}'", args.fpv_link))?;
    let plane_fc = FcLogFormat::parse(&args.plane_fc)
        .ok_or_else(|| format!("unknown --plane-fc '{}'", args.plane_fc))?;
    for a in &mut s.assets {
        match a.family {
            Family::Unknown => return Err("unknown family has no synthetic vehicle model".into()),
            Family::Fpv => {
                a.link_profile = fpv_link;
                a.handset_tmr10ms = args.handset_tmr10ms;
            }
            Family::FixedWing => a.fc_log = plane_fc,
            Family::Ugv => {}
        }
    }
    for spec in &args.drop_channels {
        let (asset_id, ch) = spec
            .split_once(':')
            .ok_or_else(|| format!("bad --drop-channel '{spec}' (ASSET:CHANNEL)"))?;
        let ch = ChannelId::parse(ch).ok_or_else(|| format!("unknown channel '{ch}'"))?;
        let a = s
            .assets
            .iter_mut()
            .find(|a| a.asset_id == asset_id)
            .ok_or_else(|| format!("unknown asset '{asset_id}'"))?;
        a.suppressed_channels.push(ch);
    }
    Ok(s)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    let scenario = build_scenario(&args)?;
    let timeline = generate(&scenario);
    fs::create_dir_all(&args.out)?;

    let assets: Vec<(String, Family)> = scenario
        .assets
        .iter()
        .map(|a| (a.asset_id.clone(), a.family))
        .collect();
    let owned = default_writers();
    let writers: Vec<&dyn FamilyWriter> = owned.iter().map(AsRef::as_ref).collect();
    let (files, skipped) = render_all(&timeline, &assets, &writers);

    let mut summary = String::new();
    summary.push_str(&format!(
        "musubi-reference-synth (P-03) seed={} t0_unix_us={} duration_ms={} events={} injections={}\n",
        scenario.seed,
        timeline.t0_unix_us,
        scenario.duration_ms,
        timeline.events.len(),
        scenario.injections.len()
    ));
    summary.push_str("SYNTHETIC: these files are generated by KECKU from public format specs; they prove nothing about any partner's files.\n");
    for a in &scenario.assets {
        summary.push_str(&format!(
            "asset {} family={} fc_log={} link={} boot_lead_us={} drift_ppm={} suppressed={}\n",
            a.asset_id,
            a.family.as_str(),
            a.fc_log.as_str(),
            a.link_profile.as_str(),
            a.boot_lead_us,
            a.drift_ppm,
            a.suppressed_channels
                .iter()
                .map(|c| c.as_str())
                .collect::<Vec<_>>()
                .join("|")
        ));
    }
    summary.push('\n');
    for f in &files {
        let path = args.out.join(&f.file_name);
        fs::write(&path, &f.bytes)?;
        summary.push_str(&format!(
            "wrote {} ({} bytes, family={}, source={}, format={})\n",
            f.file_name,
            f.bytes.len(),
            f.family.as_str(),
            f.source.as_str(),
            f.format_id
        ));
    }
    if !args.no_dvr_dir {
        for a in scenario.assets.iter().filter(|a| a.family == Family::Fpv) {
            let segs = segments_for(&timeline, &a.asset_id);
            if !segs.is_empty() {
                let dir = args.out.join(format!("{}_dvr", a.asset_id));
                materialize_dvr_dir(&dir, &segs)?;
                summary.push_str(&format!(
                    "wrote {}/ ({} segment files, presence only, zero-filled)\n",
                    dir.file_name().and_then(|n| n.to_str()).unwrap_or("dvr"),
                    segs.len()
                ));
            }
        }
    }
    for (asset, format, err) in &skipped {
        summary.push_str(&format!("skipped {asset} {format}: {err}\n"));
    }
    summary.push_str(
        "\nground truth (kind,family,asset_id,t_start_ms,t_end_ms,affected_channels,prelude_ms,prelude_order,log_end_mode,handset_loss_mode,radio_status,link_profile,missing_channels):\n",
    );
    let mut gt = String::from(
        "kind,family,asset_id,t_start_ms,t_end_ms,affected_channels,prelude_ms,prelude_order,log_end_mode,handset_loss_mode,radio_status,link_profile,missing_channels\n",
    );
    for g in &timeline.ground_truth {
        let ch: Vec<&str> = g.affected_channels.iter().map(|c| c.as_str()).collect();
        let miss: Vec<&str> = g.missing_channels.iter().map(|c| c.as_str()).collect();
        let line = format!(
            "{},{},{},{},{},{},{},{},{},{},{},{},{}",
            g.kind.as_str(),
            g.family.as_str(),
            g.asset_id,
            g.t_start_ms,
            g.t_end_ms,
            ch.join("|"),
            g.params.prelude_ms,
            match g.params.prelude_order {
                PreludeOrder::NoiseLimited => "noise_limited",
                PreludeOrder::InterferenceLimited => "interference_limited",
            },
            match g.params.log_end_mode {
                LogEndMode::Truncated => "truncated".to_string(),
                LogEndMode::Reboot { after_ms } => format!("reboot:{after_ms}"),
            },
            match g.params.handset_loss_mode {
                HandsetLossMode::ZeroWithGpsBlank => "zero_with_gps_blank",
                HandsetLossMode::Frozen => "frozen",
            },
            match g.params.radio_status {
                RadioStatusVariant::Stops => "stops",
                RadioStatusVariant::RemoteAlive => "remote_alive",
            },
            g.link_profile.as_str(),
            miss.join("|")
        );
        summary.push_str(&format!("  {line}\n"));
        gt.push_str(&line);
        gt.push('\n');
    }
    fs::write(args.out.join("ground_truth.csv"), gt)?;
    fs::write(args.out.join("SUMMARY.txt"), &summary)?;
    print!("{summary}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inject_spec_parses_kind_family_times_and_options() {
        let i = parse_injection("fc_failure,fixed_wing,90000,60000,end=reboot:20000,radio=stops")
            .expect("parses");
        assert_eq!(i.kind, FailureKind::FcFailure);
        assert_eq!(i.family, Family::FixedWing);
        assert_eq!((i.t_start_ms, i.duration_ms), (90_000, 60_000));
        assert_eq!(
            i.params.log_end_mode,
            LogEndMode::Reboot { after_ms: 20_000 }
        );
        assert_eq!(i.params.radio_status, RadioStatusVariant::Stops);
        let j = parse_injection(
            "rc_link_loss,fpv,70000,20000,handset=frozen,prelude=interference,prelude_ms=5000",
        )
        .expect("parses");
        assert_eq!(j.params.handset_loss_mode, HandsetLossMode::Frozen);
        assert_eq!(j.params.prelude_order, PreludeOrder::InterferenceLimited);
        assert_eq!(j.params.prelude_ms, 5_000);
        assert!(parse_injection("nope,fpv,0,1").is_err());
        assert!(parse_injection("camera_stop,fpv,0").is_err());
        assert!(parse_injection("camera_stop,fpv,0,1,bogus=1").is_err());
    }

    #[test]
    fn build_scenario_applies_link_profile_plane_fc_and_drop_channels() {
        let args = Args {
            out: PathBuf::from("x"),
            seed: 1,
            t0_unix_us: Some(10),
            duration_ms: Some(30_000),
            no_default_injections: true,
            injections: vec!["fiber_break,fpv,10000,5000".into()],
            fpv_link: "fiber".into(),
            plane_fc: "ardupilot".into(),
            drop_channels: vec!["ugv-01:link_stats".into()],
            handset_tmr10ms: true,
            no_dvr_dir: true,
        };
        let s = build_scenario(&args).expect("scenario");
        assert_eq!((s.t0_unix_us, s.duration_ms), (10, 30_000));
        assert_eq!(s.injections.len(), 1);
        assert_eq!(s.injections[0].kind, FailureKind::FiberBreak);
        assert_eq!(s.assets[2].link_profile, LinkProfile::Fiber);
        assert!(s.assets[2].handset_tmr10ms);
        assert_eq!(s.assets[1].fc_log, FcLogFormat::ArduPilotBin);
        assert_eq!(s.assets[0].suppressed_channels, vec![ChannelId::LinkStats]);
        let bad = Args {
            drop_channels: vec!["ugv-99:link_stats".into()],
            ..args
        };
        assert!(build_scenario(&bad).is_err());
    }
}
