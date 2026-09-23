#!/usr/bin/env python3
"""Adopted CRSF link observations + a declared link level -> observed condition rows.

A wrapper, **not a second engine**. It states which link quantities may be compared and hands
them to the crossing, run, window, hysteresis and provenance implementation of the adopted
declared-limit bridge, imported unchanged. Nothing about radios is decided here: no frame is
parsed, no radio is contacted, no packet is read, and a crossing is never a link loss, a fault
or a cause.

The level is the **engineer's or operator's explicit declaration** of a value of interest. It is
not a safety threshold, not a certification, and it is never inferred from the data.
"""
try:
    from scripts.convert_electrical_events import ELECTRICAL_DOMAIN, convert as evaluate_declared
    from scripts.convert_electrical_events import main as shared_main
except ModuleNotFoundError:
    from convert_electrical_events import ELECTRICAL_DOMAIN, convert as evaluate_declared
    from convert_electrical_events import main as shared_main

# Quantity -> (source column, declared unit, optional availability column and its sentinel), in
# the shape the shared bridge already uses. Both columns are the ones the adopted CRSF link
# converter already emits, in the unit its case already declares; nothing is rescaled, and a
# unit that does not match is refused rather than converted. A caller may name another column
# of the SAME declared unit through the bridge's existing `column` declaration -- that is how
# `uplink_rssi_2_dbm` or `downlink_rssi_dbm` is compared -- which authorises no conversion.
LINK_QUANTITY = {
    # The reported packet-success fraction. It is what the sender reported, never a measured
    # loss and never a count of anything that went missing.
    "link_quality_fraction": ("uplink_lq_fraction", "fraction", None),
    # The reported signal strength in dBm, which the case already declares may be stale when
    # there is no link. Comparing it to a declared level is the same arithmetic as above.
    "link_signal_dbm": ("uplink_rssi_1_dbm", "dBm", None),
}
LINK_CONDITION_QUANTITY = {
    "link_quality_below_declared_level": "link_quality_fraction",
    "link_signal_below_declared_level": "link_signal_dbm",
}
# Both conditions are entered by falling, so both must declare a downward comparison. The
# shared bridge refuses a declaration whose direction contradicts its condition.
LINK_DOWNWARD_CONDITIONS = set(LINK_CONDITION_QUANTITY)
LINK_INTERPRETATION = ("observed_against_a_declared_link_level_never_a_radio_loss_fault_cause_"
                       "interference_obstruction_range_antenna_or_outage_finding")
LINK_REUSE = ("consumes the normalized observations of the adopted CRSF link converter and the "
              "declared-limit implementation of the adopted electrical condition bridge; no "
              "frame is decoded, no radio is contacted and no command is emitted")
LINK_DOMAIN = {
    "quantities": LINK_QUANTITY,
    "conditions": LINK_CONDITION_QUANTITY,
    "downward": LINK_DOWNWARD_CONDITIONS,
    "interpretation": LINK_INTERPRETATION,
    "reuse": LINK_REUSE,
    "failure": "declared link level evaluation failed; no link condition is claimed",
    "input_help": "normalized CRSF link observations.csv",
}
assert set(LINK_DOMAIN) == set(ELECTRICAL_DOMAIN), "the shared bridge's domain shape is fixed"


def convert(observations, limits_text, **declarations):
    """The shared evaluation, restricted to the link quantities declared above."""
    return evaluate_declared(observations, limits_text, domain=LINK_DOMAIN, **declarations)


def main():
    shared_main(domain=LINK_DOMAIN, description=__doc__)


if __name__ == "__main__":
    main()
