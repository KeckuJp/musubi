use musubi_reference_scenario::{FcLogFormat, generate, pre_demo_default};
use musubi_reference_writers::{
    Family, FamilyWriter, SourceRole, WriteError, bin::ArduPilotBinWriter, tlog::TlogWriter,
};

#[test]
fn synthetic_writers_reject_unknown_family_even_after_known_events() {
    let original = generate(&pre_demo_default(42));
    let asset_id = original
        .assets
        .iter()
        .find(|asset| asset.fc_log == FcLogFormat::ArduPilotBin)
        .unwrap()
        .asset_id
        .clone();
    for (writer, source) in [
        (&ArduPilotBinWriter as &dyn FamilyWriter, SourceRole::Fc),
        (&TlogWriter as &dyn FamilyWriter, SourceRole::Gcs),
    ] {
        assert!(writer.render(&original, &asset_id).is_ok());
        let mut changed = original.clone();
        let event = changed
            .events
            .iter_mut()
            .rev()
            .find(|event| event.asset_id == asset_id && event.source == source)
            .unwrap();
        event.family = Family::Unknown;
        assert!(
            matches!(
                writer.render(&changed, &asset_id),
                Err(WriteError::FormatMismatch { .. })
            ),
            "{} must reject a later unknown family event",
            writer.format_id()
        );
    }
}
