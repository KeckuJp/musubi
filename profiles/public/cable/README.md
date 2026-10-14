# Public synthetic cable profile provenance

This directory defines the threshold-free public profile embedded by the deterministic cable
fixture. Its cumulative synthetic instrumentation tiers are fixed as follows:

- T0: observed motor torque, plus common vehicle telemetry;
- T1: T0 plus payout/reel channels and scalar tension measured at one end;
- T2: T1 plus vector tension at both ends and tether geometry/shape proxies.

The declared baseline is 8 N over 100 synthetic samples with standard deviation 0.08 N; nominal
tether length is 50 m; calibration scale is 1 and offset is 0. Deterministic quality fixtures also
exercise untrusted-clock, calibration-unknown, and calibration-metadata-missing abstention. This is
`SIMULATED` configuration, not a universal threshold, field calibration, physical-performance
claim, runtime-load approval, or evidence that these tiers fit a particular device.
