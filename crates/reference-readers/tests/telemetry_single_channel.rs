use musubi_reference_readers::{
    ProfileReader, observation_digest, profile::parse_profile, telemetry_csv::TelemetryCsvReader,
};
use musubi_reference_types::ChannelId;

#[test]
fn one_explicit_channel_keeps_all_fields_and_binds_the_digest() {
    for channel in [
        ChannelId::Video,
        ChannelId::LinkStats,
        ChannelId::Event,
        ChannelId::Onboard,
    ] {
        let text = format!(
            r#"profile_id="record"
version="1"
family="unknown"
source_role="recorded_export"
format="telemetry_csv_us"
extensions=["csv"]
default_clock_basis="unknown"
channels=["{}"]
[fields]
time="record_time_us"
"#,
            channel.as_str()
        );
        let profile = parse_profile(&text, "test").unwrap();
        let input = b"record_time_us,width,unknown\n1000,320,keep\n2000,640,keep2\n";
        let observations = TelemetryCsvReader.read(&profile, input).unwrap();
        assert_eq!(observations.len(), 2);
        for o in observations {
            assert_eq!(o.channel, channel);
            assert_eq!(o.fields.len(), 3);
            assert_eq!(o.digest, observation_digest(o.t_ms, channel, &o.fields));
            assert!(o.t_boot_us.is_none());
            assert!(o.anchor_unix_us.is_none());
        }
    }
}
