# AGENTS.md

Instructions for a coding agent working in this repository. A human contributor should read
[CONTRIBUTING.md](CONTRIBUTING.md); everything there applies here too.

## What this project is

Musubi turns signals from equipment you cannot fully trust into evidence you can check. It
normalizes an observation into one object model, attaches a MARK saying how much of it is
intact and why, and seals a content digest over the result.

The design rests on a small number of properties. Most of what follows is those properties
restated as things not to do, because they are easy to break by accident and expensive to
notice later.

## Commands

```
cargo build --workspace --all-targets
cargo test  --workspace
cargo fmt   --all --check
cargo clippy --workspace --all-targets
cargo deny --manifest-path crates/core/Cargo.toml check bans
```

The cargo-deny run is scoped to `core` deliberately. A run at the workspace root does not scope
per crate, so it answers a different question.

## Do not

- **Do not add a write path.** No send, connect, acknowledge, heartbeat or command back to a
  device, anywhere, including in a test helper. The tap trait has no such method by
  construction; do not add one, and do not add a type that has one.
- **Do not drop an observation.** Input that cannot be handled is refused with an INVALID MARK
  carrying its source and a specific reason. A silent `None` is the one outcome that is never
  acceptable, because it is indistinguishable from nothing having arrived.
- **Do not construct `MarkStatus::Ok` or `MarkStatus::Degraded` in an adapter.** Report quality
  facts as notes and let `musubi_core::derive_mark` decide. That vocabulary lives in one place
  so it cannot drift per adapter.
- **Do not fire a reserved MARK value from an adapter.** `OrderUnknown`, `Withheld`,
  `StaleRevocation`, `SelfAssertedTime` and `BufferSaturated` belong to other paths.
- **Do not add a network or async-runtime crate to `musubi-core`**, directly or transitively.
- **Do not write `unsafe`.**
- **Do not invent a total order.** Causally incomparable observations are marked
  `ORDER_UNKNOWN`.
- **Do not put `;` in a reason code or a provenance token.** It is the top-level separator on
  the reading side, and a token containing one cannot be split apart again.
- **Do not change a golden digest to make a test pass.** If a digest moved, either the
  serialization changed on purpose -- in which case say so in the commit message and re-pin
  deliberately -- or something broke.
- **Do not add a recorded fixture.** Fixtures are generated from a seed. A capture with its
  identifying values replaced is not synthetic.

## Claims

Do not write, in code, comments, documentation or a commit message:

- that the content digest is tamper-proof. It is tamper-evident: it detects a flipped bit, and
  without a signature an adversary can recompute it.
- that anything here is compatible with, tested against, or accepted by any product, platform,
  protocol implementation or device.
- that a synthetic fixture came from anywhere.
- that anything here is production-tested or field-tested.

If you are unsure whether a sentence is a claim the code supports, write the weaker sentence.

## Tests

A test that passes whether or not the code is right is worse than no test, because it is
counted. When you assert that something does not happen, add the case that shows the assertion
can fail. Several tests in this repository exist only for that: they are named for the property
they keep honest.
