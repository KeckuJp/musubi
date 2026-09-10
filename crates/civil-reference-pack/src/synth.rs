//! The synthesizer. Every number in this pack comes from here.
//!
//! ## Why this module exists at all
//!
//! A fixture pack can be synthetic in two very different senses. It can be generated from a
//! rule anyone can re-run, or it can be a recorded capture with the identifying values
//! replaced. The second one still carries the shape of wherever it came from -- the timing,
//! the field cadence, the correlations between channels -- and calling it synthetic is a
//! claim nobody can check.
//!
//! So this pack takes the first sense and makes it checkable: **every value in every fixture
//! is computed by the functions in this file from an integer seed.** There is no data file to
//! load, no capture to anonymise, and no input to this crate other than a `u64`. A reader who
//! doubts the provenance can read [`SplitMix64`], read the generators below, and reproduce
//! every byte the pack emits.
//!
//! ## The generator
//!
//! [`SplitMix64`] is a small, well-known counter-based mixer. It is used here because its
//! whole state is one `u64` and its step is four lines of arithmetic, so the claim "these
//! numbers came from this rule" is one a reader can verify by inspection rather than by
//! trust. It is emphatically **not** a cryptographic generator, and nothing here should be
//! used to produce keys, nonces or anything else that needs unpredictability.
//!
//! ## Determinism
//!
//! Same seed, same values, on every platform and every run: the arithmetic is fixed-width
//! integer arithmetic with wrapping semantics, and the floating-point values are derived from
//! integers by division rather than by accumulating rounding error. That is what lets a
//! fixture double as a golden.

/// A counter-based mixer with a single `u64` of state.
///
/// Not cryptographic. See the module documentation.
#[derive(Debug, Clone)]
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    /// Start the generator at `seed`.
    #[must_use]
    pub const fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    /// The next raw value.
    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// A value in `[0.0, 1.0)`.
    ///
    /// Taken from the top 53 bits and divided once, so the result is exact and carries no
    /// accumulated rounding.
    pub fn next_unit(&mut self) -> f64 {
        #[allow(clippy::cast_precision_loss)]
        let numerator = (self.next_u64() >> 11) as f64;
        numerator / (1u64 << 53) as f64
    }

    /// A value in `[low, high)`.
    ///
    /// # Panics
    /// Panics when `low` is not below `high`, or when either bound is not finite: a fixture
    /// generated from a nonsensical range is worse than no fixture, because it looks like
    /// data.
    pub fn next_in(&mut self, low: f64, high: f64) -> f64 {
        assert!(
            low.is_finite() && high.is_finite() && low < high,
            "range must be finite and non-empty: [{low}, {high})"
        );
        low + self.next_unit() * (high - low)
    }

    /// A value in `[low, high)`, rounded to `decimals` places.
    ///
    /// Fixtures round because a wire carries a fixed number of digits; rounding here rather
    /// than at the point of formatting keeps the expected value and the emitted value equal.
    ///
    /// # Panics
    /// Panics on the same conditions as [`Self::next_in`], and when `decimals` exceeds 9.
    pub fn next_rounded(&mut self, low: f64, high: f64, decimals: u32) -> f64 {
        assert!(decimals <= 9, "decimals must be at most 9, got {decimals}");
        let scale = 10f64.powi(
            i32::try_from(decimals).expect("decimals is at most 9 and therefore fits in i32"),
        );
        (self.next_in(low, high) * scale).round() / scale
    }

    /// One of `choices`.
    ///
    /// # Panics
    /// Panics when `choices` is empty.
    pub fn pick<'a, T>(&mut self, choices: &'a [T]) -> &'a T {
        assert!(!choices.is_empty(), "cannot pick from an empty slice");
        let idx = usize::try_from(self.next_u64() % choices.len() as u64)
            .expect("a value reduced modulo a usize length fits in usize");
        &choices[idx]
    }

    /// `true` with probability `p`.
    ///
    /// # Panics
    /// Panics when `p` is outside `[0.0, 1.0]`.
    pub fn chance(&mut self, p: f64) -> bool {
        assert!(
            (0.0..=1.0).contains(&p),
            "probability must be in [0, 1], got {p}"
        );
        self.next_unit() < p
    }
}

/// The bounding box every position in this pack is drawn from.
///
/// A rectangle over open farmland, chosen for one reason: it has to be somewhere, and a
/// rectangle stated here in the source is a place a reader can see the whole of. No position
/// in this pack was observed anywhere; each one is [`SplitMix64::next_rounded`] applied to
/// these bounds.
pub const FIELD_LAT: (f64, f64) = (36.100_0, 36.140_0);
/// Longitude bounds, on the same footing as [`FIELD_LAT`].
pub const FIELD_LON: (f64, f64) = (140.050_0, 140.110_0);

/// The epoch second every timeline in this pack starts at.
///
/// A fixed constant rather than a real clock, because a fixture that reads the clock is a
/// fixture that cannot be a golden.
pub const EPOCH_START_S: i64 = 1_726_300_000;

/// The reception time the pack's simulated ingest layer stamps, in epoch milliseconds.
///
/// Non-zero on purpose. A pack that stamped zero here would model, and therefore teach, the
/// exact mistake the two-time contract exists to prevent.
pub const INGEST_RECEIVED_AT_MS: i64 = 1_726_300_000_000;

#[cfg(test)]
mod tests {
    use super::{EPOCH_START_S, FIELD_LAT, FIELD_LON, INGEST_RECEIVED_AT_MS, SplitMix64};

    #[test]
    fn the_same_seed_produces_the_same_stream() {
        let a: Vec<u64> = (0..8)
            .scan(SplitMix64::new(7), |g, _| Some(g.next_u64()))
            .collect();
        let b: Vec<u64> = (0..8)
            .scan(SplitMix64::new(7), |g, _| Some(g.next_u64()))
            .collect();
        assert_eq!(a, b, "the pack is reproducible from its seed alone");
    }

    #[test]
    fn different_seeds_produce_different_streams() {
        // Non-vacuity for the test above: equality there has to mean something.
        let a: Vec<u64> = (0..8)
            .scan(SplitMix64::new(7), |g, _| Some(g.next_u64()))
            .collect();
        let b: Vec<u64> = (0..8)
            .scan(SplitMix64::new(8), |g, _| Some(g.next_u64()))
            .collect();
        assert_ne!(a, b);
    }

    #[test]
    fn the_first_values_are_pinned() {
        // A golden on the generator itself. If SplitMix64 is ever edited, every fixture in
        // this pack changes, and this is the test that says so first.
        let mut g = SplitMix64::new(0);
        assert_eq!(g.next_u64(), 16_294_208_416_658_607_535);
        assert_eq!(g.next_u64(), 7_960_286_522_194_355_700);
        assert_eq!(g.next_u64(), 487_617_019_471_545_679);
    }

    #[test]
    fn unit_values_stay_in_the_half_open_unit_interval() {
        let mut g = SplitMix64::new(42);
        for _ in 0..2_000 {
            let v = g.next_unit();
            assert!((0.0..1.0).contains(&v), "{v}");
        }
    }

    #[test]
    fn ranged_values_stay_inside_their_bounds() {
        let mut g = SplitMix64::new(11);
        for _ in 0..2_000 {
            let lat = g.next_rounded(FIELD_LAT.0, FIELD_LAT.1, 6);
            let lon = g.next_rounded(FIELD_LON.0, FIELD_LON.1, 6);
            // Rounding can land exactly on the upper bound, so the check is inclusive there.
            assert!((FIELD_LAT.0..=FIELD_LAT.1).contains(&lat), "{lat}");
            assert!((FIELD_LON.0..=FIELD_LON.1).contains(&lon), "{lon}");
        }
    }

    #[test]
    fn rounding_keeps_the_stated_number_of_decimals() {
        let mut g = SplitMix64::new(3);
        for _ in 0..500 {
            let v = g.next_rounded(0.0, 100.0, 2);
            assert!(((v * 100.0).round() - v * 100.0).abs() < 1e-9, "{v}");
        }
    }

    #[test]
    fn the_pack_reads_no_clock() {
        // The two time constants are constants. A fixture that read a real clock could not be
        // a golden, and this is the assertion that keeps them from quietly becoming one.
        assert_eq!(EPOCH_START_S, 1_726_300_000);
        assert_eq!(INGEST_RECEIVED_AT_MS, 1_726_300_000_000);
        assert!(
            INGEST_RECEIVED_AT_MS != 0,
            "a zero here would model the one mistake the transcription contract prevents"
        );
    }
}
