# Contributing

## Sign-off: DCO, not a CLA

Every commit needs a `Signed-off-by` line:

```
git commit -s -m "..."
```

That line means you certify the [Developer Certificate of Origin](https://developercertificate.org)
1.1: you wrote the change, or you have the right to submit it under the project's licence, and
you understand it is public and permanent.

We use the DCO rather than a contributor licence agreement because it asks for an assertion you
can make yourself, in one line, without a signature process. If a contributor's employer needs
something more, tell us and we will work it out then rather than imposing that overhead on
everyone in advance.

## How changes come in

Contributions arrive as pull requests against this repository and are reviewed here. This is a
one-directional intake: changes flow in, are reviewed on their merits, and are merged or
declined here.

That has one consequence worth stating plainly. **A pull request may be declined for reasons
that are not about its quality.** A change can be correct, well tested and still not land,
because it does not fit the boundary this project keeps. When that happens we will tell you it
is the reason, rather than inventing a technical objection.

## What a good change looks like

- **A test that fails before it and passes after.** For a bug, the test should reproduce the
  bug. For a behaviour change, it should be the thing that would have caught the old behaviour.
- **A non-vacuous test.** A test that passes whether or not the code is right does not test
  anything. Where a test asserts an absence, add the case that shows the assertion can fail.
- **A commit message that says why.** The diff says what changed; the message should say what
  was wrong with the old state.
- **Small and ordered.** A large change split into steps that each build and pass is far easier
  to review, and far easier to bisect later.

## What will be pushed back on

- **A silent drop.** Anything that discards an observation without producing a MARK carrying
  its source and a reason. If input cannot be handled, refuse it explicitly.
- **An adapter constructing OK or DEGRADED itself.** Report the quality facts and let the core
  derive the status, or the vocabulary drifts per adapter and two sources' marks stop being
  comparable.
- **Firing a reserved MARK value from an adapter.** Those belong to the store-and-forward,
  fan-out and signature-verification paths.
- **A write path.** No send, no command, no acknowledgement, no heartbeat back to a device.
  This is the boundary the project exists to keep, and it is not negotiable in a pull request.
- **A network or async-runtime dependency in `musubi-core`.** The cargo-deny policy refuses it,
  and the refusal is deliberate.
- **`unsafe`.** Forbidden workspace-wide.
- **An invented total order.** Causally incomparable observations are marked `ORDER_UNKNOWN`.
- **A claim the code does not support.** Do not describe the content digest as tamper-proof, do
  not claim compatibility with a product or device, and do not describe a synthetic fixture as
  though it came from somewhere.

## Before you open a pull request

```
cargo fmt --all --check
cargo clippy --workspace --all-targets
cargo test --workspace
cargo deny --manifest-path crates/core/Cargo.toml check bans
```

The last one is scoped to `core` on purpose: a run at the workspace root does not scope per
crate, so it answers a different question from the one that matters.

## Fixtures

Synthetic only, and generated rather than recorded. A fixture derived from a real capture by
replacing its identifying values still carries the timing and correlations of wherever it came
from, and it is not synthetic just because the names were changed. If your test needs data,
generate it from a seed the way `crates/civil-reference-pack/src/synth.rs` does.
