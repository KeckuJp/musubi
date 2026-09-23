#!/usr/bin/env python3
"""Saved `ip -s -j link` document -> common Observation; no command is run and no interface is read.

The engineer saves the output of iproute2's own `ip -s -j link` and passes the file here. Counter
meanings come from the kernel UAPI header iproute2 bundles at the same commit; they are reported
counts, never rates and never a fault diagnosis. Neither that header nor the kernel statistics
document states a cumulative or reset basis, so none is asserted. The statistics width the writer
chose is carried explicitly so 32-bit and 64-bit counters are never conflated.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

INPUT_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
# iproute2 v6.16.0 @915d3eafcc19706c27b220134b25c24a5b9913b3, ip/ipaddress.c and lib/utils.c.
WRITER = "iproute2@915d3eafcc19706c27b220134b25c24a5b9913b3 ip -s -j link"
# ipaddress.c:119-122; the writer emits exactly these seven spellings.
OPER_STATES = ("UNKNOWN", "NOTPRESENT", "DOWN", "LOWERLAYERDOWN", "TESTING", "DORMANT", "UP")
# ipaddress.c:662-670 and :701-710. Only keys the writer emits at a single -s are selected.
RX_COUNTERS = ("bytes", "packets", "errors", "dropped", "over_errors", "multicast")
TX_COUNTERS = ("bytes", "packets", "errors", "dropped", "carrier_errors", "collisions")
# lib/utils.c:1524-1528 picks IFLA_STATS64 else IFLA_STATS; ipaddress.c:854-855 names the object.
STATS_KINDS = ("stats64", "stats")
# struct rtnl_link_stats64 is __u64; struct rtnl_link_stats is __u32, so the widths differ.
STATS_WIDTH = {"stats64": 2**64, "stats": 2**32}
STATS_BASIS = {
    "stats64": "SIXTY_FOUR_BIT_KERNEL_COUNTERS_THE_WRITER_NAMED_THE_OBJECT_STATS64",
    "stats": "THIRTY_TWO_BIT_KERNEL_COUNTERS_WIDENED_BY_THE_WRITER_THEIR_REPRESENTABLE_RANGE_IS_"
             "TWO_TO_THE_THIRTY_TWO_AND_THEY_ARE_NOT_THE_SAME_QUANTITY_AS_STATS64",
}
# iproute2 bundles the UAPI header it builds against, so the semantics below are pinned at the very
# same commit as the writer. struct rtnl_link_stats64 fields are __u64; struct rtnl_link_stats are
# __u32. Neither the header nor Documentation/networking/statistics.rst at v6.16 states that these
# are cumulative or when they reset, so that is recorded as unestablished rather than asserted.
UAPI = "iproute2@915d3eafcc19706c27b220134b25c24a5b9913b3 include/uapi/linux/if_link.h"
COUNTER_BASIS = ("REPORTED_COUNT_AS_THE_KERNEL_UAPI_DEFINES_IT_NOT_A_RATE_NOT_A_DELTA_AND_NEVER_A_"
                 "FAULT_DIAGNOSIS_NEITHER_THE_PINNED_UAPI_HEADER_NOR_THE_KERNEL_STATISTICS_"
                 "DOCUMENT_STATES_A_CUMULATIVE_OR_RESET_BASIS_SO_NONE_IS_ASSERTED_HERE")
# Quoted from the kernel-doc of struct rtnl_link_stats64 in the pinned header.
COUNTER_MEANING = {
    "rx_bytes": "NUMBER_OF_GOOD_RECEIVED_BYTES_CORRESPONDING_TO_RX_PACKETS",
    "rx_packets": "NUMBER_OF_GOOD_PACKETS_RECEIVED_BY_THE_INTERFACE",
    "rx_errors": "TOTAL_NUMBER_OF_BAD_PACKETS_RECEIVED_AN_AGGREGATE_THAT_MUST_INCLUDE_THE_NAMED_"
                 "DETAILED_RECEIVE_ERROR_COUNTERS",
    "rx_dropped": "NUMBER_OF_PACKETS_RECEIVED_BUT_NOT_PROCESSED",
    "rx_over_errors": "RECEIVER_FIFO_OVERFLOW_EVENT_COUNTER",
    "rx_multicast": "MULTICAST_PACKETS_RECEIVED_COUNTED_AT_THE_DEVICE_LEVEL_UNLIKE_RX_PACKETS",
    "tx_bytes": "NUMBER_OF_GOOD_TRANSMITTED_BYTES_CORRESPONDING_TO_TX_PACKETS",
    "tx_packets": "NUMBER_OF_PACKETS_SUCCESSFULLY_TRANSMITTED",
    "tx_errors": "TOTAL_NUMBER_OF_TRANSMIT_PROBLEMS_AN_AGGREGATE_THAT_MUST_INCLUDE_THE_NAMED_"
                 "DETAILED_TRANSMIT_ERROR_COUNTERS",
    "tx_dropped": "NUMBER_OF_PACKETS_DROPPED_ON_THEIR_WAY_TO_TRANSMISSION",
    "tx_carrier_errors": "NUMBER_OF_FRAME_TRANSMISSION_ERRORS_DUE_TO_LOSS_OF_CARRIER",
    "tx_collisions": "NUMBER_OF_COLLISIONS_DURING_PACKET_TRANSMISSIONS",
}
STATE_BASIS = ("REPORTED_OPERATIONAL_STATE_OF_THE_INTERFACE_NOT_A_LINK_TEST_NOT_CONNECTIVITY_AND_"
               "NOT_ROUTING_OR_NAT_STATE")
SELECTED_KEYS = {"ifindex", "ifname", "operstate", "operstate_index", "mtu", "flags", "link_type",
                 *STATS_KINDS}
FIELDS = ["record_time_us", "link_ifindex", "link_ifname_hex", "link_operstate",
          "link_operstate_index", "link_state_basis", "link_mtu", "link_flags_hex",
          "link_type_hex", "link_stats_kind", "link_stats_basis", "link_counter_basis",
          *[f"link_rx_{name}" for name in RX_COUNTERS],
          *[f"link_tx_{name}" for name in TX_COUNTERS],
          "source_index", "source_sha256"]


def unique_object(pairs):
    """A repeated key would overwrite a counter or an identity, so it is refused, not resolved."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("saved document repeats an object key")
    return dict(pairs)


def strict_json(text):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_constant=lambda name: (_ for _ in ()).throw(
                          ValueError(f"saved document carries the non-JSON constant {name}")))


def counter(value, key, limit=2**31):
    """The writer emits these through print_u64, so only a non-negative integer is a value.

    `limit` is the width the pinned UAPI declares for that field. The common signed range is a
    second, independent ceiling: a value inside its declared width can still exceed what the common
    output can carry, and that is refused rather than truncated.
    """
    if type(value) is not int or isinstance(value, bool):
        raise ValueError(f"declared unsigned value {key} must be a JSON integer")
    if value < 0:
        raise ValueError(f"declared unsigned value {key} cannot be negative")
    if value >= limit:
        raise ValueError(f"{key} exceeds the width the pinned source declares for it")
    if value >= 2**63:
        raise ValueError(f"{key} exceeds the common signed range")
    return value


def convert(raw, capture_time_us):
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("empty or oversized saved link document")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    document = strict_json(raw.decode("utf-8"))
    if not isinstance(document, list) or not document:
        raise ValueError("saved ip -j link output is a nonempty array of link objects")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    report = {"source_links": 0, "output_records": 0, "unmapped_keys": [],
              "absent_selected_keys": [], "links_without_statistics": 0,
              "writer": WRITER, "uapi": UAPI, "counter_meaning": COUNTER_MEANING,
              "source_sha256": digest,
              "clock": "CALLER_CAPTURE_ORDERS_COMMON_OUTPUT; the saved document carries no "
                       "timestamp of its own, so none is invented",
              "counter_basis": COUNTER_BASIS, "state_basis": STATE_BASIS,
              "carrier": "NOT_DECLARED_BY_THIS_WRITER_the_json_output_emits_no_carrier_key_and_the "
                         "only_carrier_literal_in_the_source_is_a_text_column_header",
              "evidence": "saved output of a pinned public writer, not a device reading, not a "
                          "connectivity test and not a topology statement"}
    seen = set()
    for index, link in enumerate(document):
        if not isinstance(link, dict):
            raise ValueError("each saved link entry must be an object")
        report["source_links"] += 1
        for key in SELECTED_KEYS:
            # The writer never emits JSON null for a selected key, so a null is malformed here
            # rather than a silent blank that would look like an honest absence.
            if key in link and link[key] is None:
                raise ValueError(f"saved link carries an explicit null for the selected key {key}")
        ifindex = link.get("ifindex")
        # struct ifinfomsg declares `int ifi_index`, so the writer cannot emit beyond a signed 32.
        if ifindex is None:
            raise ValueError("saved link requires an ifindex")
        ifindex = counter(ifindex, "ifindex", 2**31)
        if ifindex in seen:
            raise ValueError("saved document repeats an ifindex")
        seen.add(ifindex)
        ifname = link.get("ifname")
        if not isinstance(ifname, str) or not ifname:
            raise ValueError("saved link requires a named interface")
        if "operstate" in link and "operstate_index" in link:
            # The writer emits one or the other, never both (ipaddress.c:128 versus :134).
            raise ValueError("saved link declares both operstate and operstate_index")
        operstate = link.get("operstate")
        if operstate is not None and operstate not in OPER_STATES:
            raise ValueError("operstate must be one of the spellings the writer emits")
        operstate_index = link.get("operstate_index")
        if operstate_index is not None:
            # print_operstate takes a __u8, so the writer cannot emit beyond 255.
            operstate_index = counter(operstate_index, "operstate_index", 2**8)
        mtu = link.get("mtu")
        if mtu is not None:
            # IFLA_MTU is read with rta_getattr_u32.
            mtu = counter(mtu, "mtu", 2**32)
        flags = link.get("flags")
        if flags is not None and (not isinstance(flags, list)
                                  or not all(isinstance(flag, str) for flag in flags)):
            raise ValueError("declared flags must be an array of strings")
        link_type = link.get("link_type")
        if link_type is not None and not isinstance(link_type, str):
            raise ValueError("declared link_type must be a string")

        present = [kind for kind in STATS_KINDS if kind in link]
        if len(present) > 1:
            raise ValueError("saved link carries more than one statistics object")
        row = {"record_time_us": capture_time_us, "link_ifindex": ifindex,
               "link_ifname_hex": "hex:" + ifname.encode().hex(),
               "link_operstate": operstate if operstate is not None else "",
               "link_operstate_index": operstate_index if operstate_index is not None else "",
               "link_state_basis": STATE_BASIS, "link_mtu": mtu if mtu is not None else "",
               "link_flags_hex": "hex:" + ",".join(flags).encode().hex() if flags is not None else "",
               "link_type_hex": "hex:" + link_type.encode().hex() if link_type is not None else "",
               "link_counter_basis": COUNTER_BASIS, "source_index": index,
               "source_sha256": digest}
        for name in RX_COUNTERS:
            row[f"link_rx_{name}"] = ""
        for name in TX_COUNTERS:
            row[f"link_tx_{name}"] = ""
        if present:
            kind = present[0]
            statistics = link[kind]
            if not isinstance(statistics, dict):
                raise ValueError("declared statistics must be an object")
            row["link_stats_kind"] = kind
            row["link_stats_basis"] = STATS_BASIS[kind]
            width = STATS_WIDTH[kind]
            # Keys sitting directly in the statistics object, outside rx and tx, are counted too.
            report["unmapped_keys"] += [f"{ifname}.{kind}.{key}" for key in statistics
                                        if key not in ("rx", "tx")]
            for side, names in (("rx", RX_COUNTERS), ("tx", TX_COUNTERS)):
                values = statistics.get(side)
                if values is None:
                    report["absent_selected_keys"].append(f"{ifname}.{kind}.{side}")
                    continue
                if not isinstance(values, dict):
                    raise ValueError("declared rx and tx statistics must be objects")
                for name in names:
                    if name not in values:
                        # Absent stays blank and counted; it is never written as a zero.
                        report["absent_selected_keys"].append(f"{ifname}.{kind}.{side}.{name}")
                        continue
                    row[f"link_{side}_{name}"] = counter(values[name],
                                                        f"{kind}.{side}.{name}", width)
                report["unmapped_keys"] += [f"{ifname}.{kind}.{side}.{key}" for key in values
                                            if key not in names]
        else:
            report["links_without_statistics"] += 1
            row["link_stats_kind"] = ""
            row["link_stats_basis"] = ""
        report["unmapped_keys"] += [f"{ifname}.{key}" for key in link if key not in SELECTED_KEYS]
        for key in ("operstate", "mtu", "flags", "link_type"):
            if key not in link:
                report["absent_selected_keys"].append(f"{ifname}.{key}")
        writer.writerow(row)
        report["output_records"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted link state exceeds bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved output of `ip -s -j link`")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--capture-time-us", type=int, required=True)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as source:
            raw = source.read(INPUT_LIMIT + 1)
        converted, report = convert(raw, args.capture_time_us)
        args.output_directory.mkdir()
        (args.output_directory / "source.json").write_bytes(raw)
        (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2) + "\n",
                                                           encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError,
            UnicodeDecodeError, json.JSONDecodeError):
        parser.exit(2, "Saved link state conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
