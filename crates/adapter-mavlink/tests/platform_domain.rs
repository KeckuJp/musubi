//! Source-declared domain must be sealed before northbound serialization (contract).
use musubi_adapter_mavlink::MavlinkNormalizer;
use musubi_core::{Normalizer, RawObservation, verify_digest};
use musubi_northbound::{CotPublisher, NorthboundPublisher};
use musubi_types::{ComObject, MarkStatus, PlatformDomain};

fn golden() -> RawObservation {
    RawObservation {
        source_id: "uav-01".into(),
        received_at: 0,
        payload: vec![
            0xFD, 0x1C, 0, 0, 0, 1, 1, 0x21, 0, 0, 0x39, 0x30, 0, 0, 0x4C, 0x52, 0x40, 0x1C, 0x44,
            0xF4, 0x17, 0x05, 0x40, 0x72, 0x07, 0, 0x50, 0xC3, 0, 0, 0x64, 0, 0, 0, 0xF6, 0xFF,
            0x78, 0x69, 0x21, 0xA3,
        ],
    }
}

#[test]
fn legacy_constructors_preserve_explicit_air_evidence_and_xml() {
    let explicit = MavlinkNormalizer::with_platform_domain(PlatformDomain::Air)
        .normalize(&golden())
        .unwrap();
    let publisher = CotPublisher::new();
    for normalizer in [MavlinkNormalizer::new(), MavlinkNormalizer::default()] {
        let legacy = normalizer.normalize(&golden()).unwrap();
        assert_eq!(legacy, explicit);
        let xml = publisher.publish(&legacy).unwrap();
        assert_eq!(xml, publisher.publish(&explicit).unwrap());
        assert!(
            std::str::from_utf8(&xml)
                .unwrap()
                .contains("type=\"a-u-A\"")
        );
    }
}

#[test]
fn ground_domain_is_sealed_and_rendered_without_relabeling_source_data() {
    let air = MavlinkNormalizer::new().normalize(&golden()).unwrap();
    let ground = MavlinkNormalizer::with_platform_domain(PlatformDomain::Ground)
        .normalize(&golden())
        .unwrap();
    let ComObject::PlatformState(ps) = &ground.observation else {
        panic!("expected platform state");
    };
    assert_eq!(ps.platform_domain, PlatformDomain::Ground);
    assert_eq!(
        ps.platform_id, "uav-01/sys1",
        "source labels do not select the domain"
    );
    assert_eq!(verify_digest(&ground).status, MarkStatus::Ok);
    assert_ne!(ground.content_digest, air.content_digest);
    assert_eq!(ground.claim, air.claim);
    let xml = String::from_utf8(CotPublisher::new().publish(&ground).unwrap()).unwrap();
    assert!(xml.contains("type=\"a-u-G\""));
    assert!(xml.contains("lat=\"47.397742\""));
    let mut tampered = ground;
    let ComObject::PlatformState(ps) = &mut tampered.observation else {
        unreachable!()
    };
    ps.platform_domain = PlatformDomain::Air;
    assert_eq!(verify_digest(&tampered).status, MarkStatus::Invalid);
}
