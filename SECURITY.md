# Security policy

## Reporting a vulnerability

Email **oss@kecku.com** with `SECURITY` in the subject. Please do not open a public issue for
something that is exploitable.

Tell us what you can: the version or commit, what you did, what happened, and what you expected.
A proof of concept helps and is not required -- a clear description of the reasoning is often
enough to reproduce.

We will acknowledge within five working days and tell you whether we consider it a
vulnerability, along with why. If we disagree with your assessment we will say so plainly
rather than going quiet.

## What is in scope

The crates in this repository. In particular we want to hear about:

- **Anything that gets a write, a command or a network call out of a crate that declares it has
  none.** The read-only property is load-bearing here, and a way around it is the most serious
  class of bug this project can have.
- **A way to make the content digest agree with content it does not cover.** A field that
  should be inside the canonical bytes and is not, for instance.
- **A way to get an observation silently dropped**, rather than surfaced as INVALID with its
  source attached.
- **Input that panics, hangs or exhausts memory** rather than being refused at the quarantine
  boundary.
- **A way to make the conformance harness pass an adapter that violates an invariant it claims
  to check.** A check that can be fooled is worse than no check, because it is trusted.

## What is out of scope, and why

- **Adversarial resistance of the content digest on its own.** Without a signature, an
  adversary who alters content can recompute the digest. This is stated in the code and in the
  README; it is a known limit rather than a finding. Signing is not in this preview.
- **Byzantine behaviour of a source.** A source that deliberately emits contradictory causal
  clocks, or a compromised node injecting fabricated observations, is outside what the
  ordering machinery claims to handle. It handles natural loss and reordering.
- **Denial of service against a transport.** There is no transport in this repository.
- **Findings that depend on a deployment we do not ship**, such as a particular process
  supervisor or storage layer.

If you are unsure which side of the line something falls on, report it. We would rather read a
report about a known limit than miss one about a real hole.

## Disclosure

We will agree a timeline with you. Our default is to publish a fix and an advisory together,
and to credit you unless you ask us not to.
