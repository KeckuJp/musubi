use musubi_reference_pipeline::export::{absences_csv, claims_csv};
use musubi_reference_pipeline::{Knowledge, run_pipeline};
use musubi_reference_scenario::{generate, pre_demo_default};
fn hash(text: &str) -> u64 {
    text.bytes().fold(0xcbf29ce484222325, |h, byte| {
        (h ^ u64::from(byte)).wrapping_mul(0x100000001b3)
    })
}
#[test]
fn legacy_csv_bytes_are_unchanged() {
    let out = run_pipeline(&generate(&pre_demo_default(42)));
    let k = Knowledge::repo_public();
    assert_eq!(
        (hash(&absences_csv(&out)), hash(&claims_csv(&out, &k))),
        // Established independently from the previously published source tree.
        // The public catalog/build labels differ from the original development fixture.
        (7203645602732266339, 17980847890945523517)
    );
}
