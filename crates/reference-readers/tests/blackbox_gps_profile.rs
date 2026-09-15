use musubi_reference_readers::{
    ChannelId, FieldValue, ProfileReader, blackbox::BlackboxCsvReader, field,
    profile::parse_profile,
};

#[test]
fn shipped_profile_maps_optional_gps_quality_fields_to_gps_channel() {
    let profile = parse_profile(
        include_str!("../../../profiles/public/betaflight_blackbox_csv.toml"),
        "public",
    )
    .expect("shipped profile");
    let observations = BlackboxCsvReader
        .read(
            &profile,
            b"time (us),GPS_numSat,GPS_fixType,GPS_hdop\n1000000,9,3,1.25\n",
        )
        .expect("CSV with optional GPS quality fields");
    let gps = observations
        .iter()
        .find(|observation| observation.channel == ChannelId::GpsEkf)
        .expect("GPS channel");
    assert_eq!(field(gps, "GPS_fixType"), Some(&FieldValue::I64(3)));
    assert_eq!(field(gps, "GPS_hdop"), Some(&FieldValue::F64(1.25)));
}

#[test]
fn shipped_profile_accepts_absent_optional_gps_quality_fields() {
    let profile = parse_profile(
        include_str!("../../../profiles/public/betaflight_blackbox_csv.toml"),
        "public",
    )
    .expect("shipped profile");
    let observations = BlackboxCsvReader
        .read(&profile, b"time (us),GPS_numSat\n1000000,9\n")
        .expect("CSV without optional GPS quality fields");
    let gps = observations
        .iter()
        .find(|observation| observation.channel == ChannelId::GpsEkf)
        .expect("GPS channel");
    assert_eq!(field(gps, "GPS_numSat"), Some(&FieldValue::I64(9)));
    assert_eq!(field(gps, "GPS_fixType"), None);
    assert_eq!(field(gps, "GPS_hdop"), None);
}
