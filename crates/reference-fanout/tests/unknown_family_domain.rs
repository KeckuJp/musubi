use musubi_reference_fanout::domain_of;
use musubi_reference_types::Family;
use musubi_types::PlatformDomain;

#[test]
fn unspecified_recorded_family_never_becomes_air_or_ground() {
    let family = Family::parse("unknown").expect("explicit unknown family must parse");
    assert_eq!(domain_of(family), PlatformDomain::Unknown);
}
