# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project does
not yet follow semantic versioning, because it has not yet made a compatibility promise to
version against.

## [Unreleased]

The first source preview. Nothing has been released yet, so everything below is an initial
state rather than a change from one.

### Added

- An opt-in bounded position-CSV batch wrapper. Complete records retain source
  order and fields, with part hashes and record offsets; existing single-file,
  reader and core limits remain unchanged. Includes synthetic boundary tests.

- Two explicit GPS CSV layout profiles, synthetic regressions and a migration/reuse
  recipe. Reuses the existing converter, reader and public adapter without runtime changes.

- An explicit position-CSV converter, with source-field retention, boot-counter and
  coordinate-unit declarations, public common-model replay and a reusable development
  recipe. No core, reader, model or dependency change.
- Optional separate GPS-stream export in the existing offline recording stage, without
  merging held GPS values into main rows. Includes synthetic corruption/accounting tests;
  an external decoder is still required and is not bundled.

- A profile-only GPS schema migration example and synthetic public-adapter
  regressions for renamed boot counters and changed units. No runtime changes.

- An explicit offline JSONL counter remapper and migration recipe. Caller-declared
  boot counters are normalized without changing the reader or core; complete source
  records remain available as unsealed sidecars. Includes synthetic regression tests.

- A recorded JSONL adapter example connecting the existing reader to the public
  common model, core-derived MARK and sealed envelope. Includes an explicit mapping
  profile, retained-record accounting, failure tests and a reusable development recipe.

- Offline whitespace pose-text conversion with comment accounting and named numeric
  extensions. Explicit non-decreasing timestamp parsing preserves equal-time rows
  without changing the strict default. Includes a CSV inspection example and a
  standalone reproduction recipe; no common-model pose mapping is included.

- `musubi-miniseed2`: a standalone recorded-file decoder for the big-endian Steim-1/2 subset,
  an offline inspection example and synthetic tests. No recording, third-party decoder or
  equipment-specific common-model mapping is included.

- `musubi-types`: the Common Object Model subset -- timestamps with observation and reception
  time held apart, position, platform domain and state, track, payload feed, health -- together
  with the MARK envelope and the Evidence envelope. Pure data; no value in the crate carries a
  method.
- `musubi-core`: deterministic normalisation, MARK derivation from quality facts, canonical
  bytes and a content digest over them, verification of that digest on receipt, a quarantine
  boundary for untrusted input, a vector clock for causal comparison, and a bounded
  store-and-forward buffer that holds observations across a broken link and drains them in
  causal order without dropping any.
- `musubi-adapter-spi`: the adapter contract -- a read-only source manifest, a tap trait with no
  send method, and the shared vocabulary for refusing input -- plus one worked adapter over a
  synthetic wire.
- `musubi-conformance`: a harness that runs any adapter against golden and adversarial fixtures
  and checks the structural invariants, plus a static scan for a write surface the adapter has
  not declared.
- `musubi-civil-reference-pack`: seven synthetic sources and six consumer views, generated from
  an integer seed, so the whole pipeline can be exercised offline with no other data.

### Known limits

Stated here as well as in the README, because a changelog is where someone looks to find out
what a version does not do yet.

- The content digest is tamper-evident, not tamper-proof: without a signature, an adversary can
  recompute it. Signing and key management are not in this preview.
- The Common Object Model enumeration carries one variant, `PlatformState`. Sensors in the
  reference pack therefore report about the platform that carries them.
- No binary is published, and no compatibility with any product, platform or device is claimed
  or tested.
- The API is not stable. Pin an exact revision.
