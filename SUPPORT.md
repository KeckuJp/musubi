# Support

## What this is

A source preview. It is published so that it can be read, built, run and criticised -- not
because it is finished, and not with a support commitment attached.

Being direct about that is more useful than a promise nobody can hold to:

- **No service-level commitment.** No guaranteed response time, no guaranteed fix.
- **No release cadence yet.** There is no schedule to plan around.
- **No API stability yet.** Types and function signatures will change. Pin an exact revision if
  you build on this.
- **No production deployment is known to us.** If you are considering one, read the "What this
  preview is not" section of the README first, and treat every item there as a real constraint
  rather than boilerplate.

## Where to ask

| you want to | go to |
|---|---|
| report a bug, or something that behaved differently from its documentation | a repository issue |
| propose a change | read [CONTRIBUTING.md](CONTRIBUTING.md) first |
| report a vulnerability | [SECURITY.md](SECURITY.md), not a public issue |
| understand a design decision | the module documentation. Most of the reasoning is written next to the code it explains rather than in a separate document that drifts from it. |

## Asking a question well

The most useful reports say what you expected and why. "This returned DEGRADED and I expected
OK" is answerable; "this seems broken" usually is not.

If your question is about an adapter you are writing, the conformance harness is likely to
answer it faster than we can: it names the invariant that failed and the fixture that failed it.
