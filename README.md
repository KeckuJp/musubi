# Musubi

Musubi turns signals from equipment you cannot fully trust into **evidence you can check**.

A sensor, a vehicle or a human report tells you something. Musubi normalizes that into one
object model, attaches a MARK saying how much of it is intact and why, seals a content digest
over the result, and hands it on. What it never does is take the reading at face value, and
what it never does is quietly drop one.

This is a **source preview**. See [What this preview is not](#what-this-preview-is-not) before
you rely on anything here.

## The one idea

Most integration middleware answers "what is the value?". Musubi answers a second question
alongside it: **"how much of this can you rely on, and what happened to the rest?"**

Three things follow from taking that seriously:

- **A failure is an object, not an absence.** A malformed frame becomes an INVALID mark with
  the source and the specific reason attached. A missing altitude becomes a DEGRADED mark that
  says `field-missing:alt`. Nothing is dropped, because a dropped observation is
  indistinguishable from one that never arrived.
- **Quality is derived in one place.** An adapter reports facts about what it saw; the core
  decides OK versus DEGRADED from those facts. If each adapter decided for itself, the
  vocabulary would drift and two sources' marks would stop being comparable.
- **Order is not invented.** When two updates are causally incomparable, they are marked
  `ORDER_UNKNOWN` rather than sorted into a line that was never observed.

## What is here

| crate | what it is |
|---|---|
| `musubi-miniseed2` | An offline, dependency-free reader for big-endian miniSEED2 Steim-1/2 records. Retains raw blockettes and declared time metadata; returns instrument counts, not calibrated physical values. See [reader usage and limits](crates/miniseed2/README.md). |
| `musubi-types` | The Common Object Model subset and the Evidence envelope. Pure data: no value in the crate carries a method. |
| `musubi-core` | Deterministic normalisation, MARK derivation, canonical bytes and the content digest, the quarantine boundary for untrusted input, and a store-and-forward buffer that holds observations across a broken link and drains them in causal order. No network crate in its dependency closure. |
| `musubi-adapter-spi` | The contract an adapter implements, plus one worked adapter over a synthetic wire. Copy it and rewrite one function. |
| `musubi-decoded-csv` | A dependency-free reader for unquoted decoded telemetry CSV. Keeps unknown columns and units, and rejects malformed rows or unsupported time units. See `crates/decoded-csv/README.md`. |
| `musubi-jsonl-log` | An offline reader for decoded JSONL exports. Retains unknown fields and untimed records alongside explicit boot-time indices; does not infer units or platform identity. See `crates/jsonl-log/README.md`. |
| `musubi-conformance` | A harness that checks any adapter against the structural invariants, including a static scan for a write surface the adapter has not declared. Runnable against your own crate. |
| `musubi-civil-reference-pack` | Seven synthetic sources -- an agricultural aircraft and ground vehicle, a camera, a multispectral head, an RTK base, a weather station, health telemetry -- and six consumer views, so you can exercise all of the above without any other data. |

## Try it

For whitespace-separated position/quaternion records with explicit equal-time
handling and comment accounting, see [Recorded pose text](docs/recorded-pose-text.md).
This reuses the CSV table reader; it does not provide a pose-to-Observation adapter.

For the optional operator-installed raw-recording to CSV stage, see
[Offline raw-recording to CSV stage](docs/recorded-blackbox.md).
It stops at decoded CSV and does not establish device compatibility.

For a recorded CSV with a known seconds column, see
[Recorded CSV time-column conversion](docs/recorded-trajectory.md).
The small conversion stage reuses the existing table reader without inferring clock quality.

```
cargo test --workspace
```

Everything runs offline and deterministically. There is no service to sign up for, no key to
obtain and no network call in any test.

To see the pipeline end to end:

```rust
use musubi_civil_reference_pack::{ReferencePack, consumers};

let pack = ReferencePack::default();
let evidence = pack.evidence();

let view = consumers::disaster_view(&evidence);
println!(
    "{} usable, {} degraded, {} sealed, {} without an absolute time",
    view.ledger.ok, view.ledger.degraded, view.sealed, view.without_absolute_time
);
assert_eq!(view.ledger.total(), evidence.len()); // nothing was dropped on the way through
```

To write an adapter for your own device, read `crates/adapter-spi/src/template.rs` and rewrite
`parse_my_device`. Then run the conformance harness against it.

## Read-only, by construction

Musubi does not send anything back to a device. That is not a policy note; it is a property of
the code that you can check:

- The tap trait has no send, connect or acknowledge method. An implementation cannot acquire
  one without changing the trait, which is a reviewable event.
- The source manifest declares zero egress, no heartbeat and no command, and a test asserts it.
- The conformance harness scans an adapter's own source for public functions outside a reviewed
  list, and fails closed on anything it has not been told about.
- No crate in this workspace uses `unsafe`. The lint forbids it and CI scans for the token,
  because a lint a crate can opt out of is not a floor.

## What this preview is not

Being specific about this is part of the work rather than a disclaimer.

- **Source only.** No binary is published, and none is claimed to work anywhere.
- **No compatibility claim.** Musubi makes no claim of compatibility with any product,
  platform, protocol implementation or device. Nothing here has been tested against one.
- **The fixtures are synthetic, and provably so.** Every value in the reference pack is
  computed from an integer seed by `crates/civil-reference-pack/src/synth.rs`. There is no data
  file, nothing is loaded at run time, and nothing is a recording with its identifying values
  replaced. Read the generator and reproduce every byte.
- **Tamper-evident, not tamper-proof.** The content digest detects a flipped bit or a
  transmission error. Without a signature, an adversary can recompute it to match altered
  content. Signatures and key management are not in this preview, and the code says so where it
  would otherwise be easy to assume otherwise.
- **No independent-maintenance claim.** Whether someone outside the original authors can
  maintain this has not been demonstrated.
- **Not a control system.** Musubi originates and transports no instruction to any device.

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Contributing, support, security

[CONTRIBUTING.md](CONTRIBUTING.md), [SUPPORT.md](SUPPORT.md), [SECURITY.md](SECURITY.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
