#!/usr/bin/env python3
"""Saved RTCP sender reports -> the one timestamp relationship a stream actually declares.

Reads RTCP packets that are already inside a saved PCAP capture. No socket is opened, no session
is created, no device is contacted, nothing is decrypted, no payload is rendered and no media is
decoded. This is the missing half of the adopted RTP path, which says of itself:
`time_basis = "RTP_SOURCE_RELATIVE_TICKS_NOT_UTC_NO_RTCP_ANCHOR_CONSUMED"` - the media clock and
the capture clock are both present there and have **no declared relationship at all**.

RFC 3550 §6.4.1 is the only place a stream states one: a sender report pairs the sender's own
wallclock time with its own RTP timestamp. That pairing is what this module carries, with the
pin's own qualifications attached:

* the NTP timestamp "Indicates the wallclock time … when this report was sent", but "The
  measurement uncertainty of the timestamp is not indicated as it may not be known";
* a sender with no wallclock "MAY use that clock" - system uptime - "to calculate **relative**
  NTP timestamps", and one with no notion of time at all "MAY set the NTP timestamp to zero";
  the pin separates those two forms by the high bit only "**Until the year 2036**", so the bit
  is reported and the form stays unresolved unless a caller declares it from a source that
  states it - it is never inferred from the bit and never from the capture clock;
* the RTP timestamp "Corresponds to the same time as the NTP timestamp (above)", and the
  correspondence serves synchronisation "for sources whose **NTP timestamps are synchronised**".

So no UTC is computed, no epoch is applied, no leap second is applied, and the pairing is
declared to be between the **sender's own two clocks**. It is never an arrival time, never the
receiver's clock, never verified, and the difference between it and the capture clock is **not
latency**: those two clocks have no declared alignment, and a recorded sample without one cannot
become end-to-end latency.

Reception report blocks are counted, never decoded: loss, interarrival jitter, LSR and DLSR are
what a *receiver* observed about someone else, and reading them here would turn a passive record
into a quality verdict. No jitter, no loss rate and no round trip is computed anywhere.

The saved-capture framing is the admitted one: this module calls `convert_imc_sensors`'s
`read_pcap_records` and `iter_udp_datagrams`, the shared walker the passive converters already
use, so there is exactly one implementation of those rules.

Pinned source: RFC 3550, Standards Track, July 2003 (`rfc3550.txt`, 259985 bytes, sha256
`4c210e9434b5b4c029e8536ad8991f3709bc3cbfa0999e951bcb4c2143c539e8`).
Contract and limits: docs/saved-inputs.md.
"""
import argparse
import csv
import decimal
import hashlib
import io
import json
from pathlib import Path
import struct

try:  # the same import shape the existing converters use for each other
    from scripts.convert_imc_sensors import (LIMIT, OUTPUT_LIMIT, iter_udp_datagrams,
                                             read_pcap_records)
except ModuleNotFoundError:  # running the script directly from scripts/
    from convert_imc_sensors import LIMIT, OUTPUT_LIMIT, iter_udp_datagrams, read_pcap_records

REFERENCE = "RFC 3550 (Standards Track, July 2003)"
RTCP_VERSION = 2
COMMON_HEADER = 4          # V/P/count, PT, length
SENDER_INFO = 20           # NTP 8, RTP timestamp 4, packet count 4, octet count 4
REPORT_BLOCK = 24          # §6.4.1 reception report block
SENDER_REPORT = 200        # §6.4.1
RECEIVER_REPORT = 201      # §6.4.2
SOURCE_DESCRIPTION = 202   # §6.5
GOODBYE = 203              # §6.6
APPLICATION_DEFINED = 204  # §6.7
NAMED_TYPES = {SENDER_REPORT: "SR", RECEIVER_REPORT: "RR", SOURCE_DESCRIPTION: "SDES",
               GOODBYE: "BYE", APPLICATION_DEFINED: "APP"}
CNAME_ITEM = 1             # §6.5.1, the only SDES item this path interprets
NTP_FRACTION = 1 << 32
# §6.4.1 says only that "**Until the year 2036**, relative and absolute timestamps will differ in
# the high bit". That is a bounded statement, and this converter has no era of its own and never
# takes one from the capture clock, so the bit is reported and decides nothing.
NTP_HIGH_BIT = 1 << 31
# A caller may declare the form from a source that states it - a producer's own documentation -
# which is a declaration, never a proof read out of the packet.
DECLARED_FORMS = ("ABSOLUTE", "RELATIVE")
MAX_CNAME_BYTES = 255      # §6.5: "the text can be no longer than 255 octets"
# This path's own bounds, not limits the pinned source states.
MAX_ROWS = 100_000
MAX_PACKETS_PER_DATAGRAM = 64

CAPTURE_BASIS = "CAPTURE_REPORTED_NOT_AUTHENTICATED"
PAIRING_BASIS = ("THE_SENDERS_OWN_TWO_CLOCKS_PAIRED_BY_ITSELF_AS_THE_PINNED_SECTION_DEFINES_"
                 "NEVER_AN_ARRIVAL_TIME_NEVER_THE_RECEIVERS_CLOCK_NEVER_VERIFIED_AND_THE_"
                 "DIFFERENCE_BETWEEN_IT_AND_THE_CAPTURE_CLOCK_IS_NOT_LATENCY_BECAUSE_THE_TWO_"
                 "CLOCKS_HAVE_NO_DECLARED_ALIGNMENT_AND_A_PAIRING_ALONE_DOES_NOT_CLOSE_A_LOW_"
                 "LATENCY_FUNCTION")
NTP_BASIS = ("REPORTED_IN_THE_NTP_FORMAT_THE_PINNED_SECTION_NAMES_CARRIED_AS_WRITTEN_WITH_NO_"
             "EPOCH_APPLIED_NO_ERA_RESOLVED_NO_UTC_CLAIMED_AND_NO_LEAP_SECOND_APPLIED_AND_ITS_"
             "MEASUREMENT_UNCERTAINTY_IS_NOT_INDICATED_BY_THE_SOURCE")
FORM_BASIS = ("THE_HIGH_BIT_IS_REPORTED_AND_PROVES_NOTHING_HERE_BECAUSE_THE_PINNED_SENTENCE_"
              "THAT_SEPARATES_THE_ABSOLUTE_AND_RELATIVE_FORMS_BY_IT_HOLDS_ONLY_UNTIL_THE_YEAR_"
              "2036_AND_THIS_PATH_HAS_NO_ERA_OF_ITS_OWN_AND_NEVER_TAKES_ONE_FROM_THE_CAPTURE_"
              "CLOCK_SO_THE_FORM_STAYS_UNRESOLVED_UNLESS_A_CALLER_DECLARES_IT_FROM_A_SOURCE_"
              "THAT_STATES_IT")
COUNT_BASIS = ("TOTALS_THE_SENDER_REPORTS_ABOUT_ITS_OWN_TRANSMISSION_SINCE_IT_STARTED_NEVER_"
               "WHAT_ARRIVED_NEVER_A_RATE_AND_NEVER_A_LOSS_MEASUREMENT")
BLOCK_BASIS = ("RECEPTION_REPORT_BLOCKS_ARE_COUNTED_AND_NOT_DECODED_HERE_BECAUSE_THEY_ARE_"
               "OUTSIDE_THE_SCOPE_OF_THIS_PATH_THEY_ARE_VALUES_A_RECEIVER_REPORTED_ABOUT_"
               "ANOTHER_SOURCE_AND_CARRYING_THEM_WOULD_BE_A_SEPARATE_CHANGE_WHICH_WOULD_STILL_"
               "REPORT_THEM_AND_NEVER_REACH_A_QUALITY_VERDICT")
CNAME_BASIS = ("THE_CANONICAL_NAME_THE_SENDER_DECLARED_FOR_THIS_SYNCHRONISATION_SOURCE_CARRIED_"
               "AS_UTF8_HEX_NEVER_PARSED_NEVER_RESOLVED_NEVER_AUTHENTICATED_AND_NEVER_A_DEVICE_"
               "IDENTITY")
RECORD_BASIS = ("every_row_is_an_OBSERVED_RECORD_read_from_a_saved_capture_it_is_never_a_"
                "transmission_never_a_session_never_a_decoded_medium_and_never_a_measured_"
                "transport_performance")

COLUMNS = ("record_time_us", "pcap_clock_basis", "pcap_packet_index", "pcap_source_sha256",
           "rtcp_record_kind", "rtcp_source_address", "rtcp_source_port",
           "rtcp_destination_address", "rtcp_destination_port", "rtcp_packet_index_in_datagram",
           "rtcp_packet_type", "rtcp_packet_type_name", "rtcp_length_words",
           "rtcp_padding_declared", "rtcp_padding_bytes_excluded", "rtcp_ssrc",
           "sr_ntp_seconds_reported", "sr_ntp_fraction_reported", "sr_ntp_seconds_text",
           "sr_ntp_high_bit_reported", "sr_ntp_form", "sr_ntp_form_basis", "sr_ntp_basis",
           "sr_rtp_timestamp_reported", "sr_media_sender_clock_pairing", "sr_pairing_basis",
           "sr_sender_packet_count_reported", "sr_sender_octet_count_reported",
           "sr_sender_count_basis", "sr_reception_report_blocks_declared",
           "sr_profile_extension_bytes", "sr_reception_report_basis",
           "sdes_cname_hex", "sdes_uninterpreted_items_in_chunk",
           "sdes_unconsumed_trailing_bytes", "sdes_cname_basis",
           "rtcp_packet_sha256", "record_basis")


def compound_packets(payload):
    """Every individual RTCP packet in one datagram, in order, with its declared bounds.

    §6.4.1 defines `length` as "the length of this RTCP packet in 32-bit words minus one,
    including the header and any padding", which is why a zero length is valid and why the walk
    below cannot loop forever. A packet whose declared length leaves the datagram is refused
    rather than truncated: a saved capture either carries the packet it declares or it does not.
    """
    found, offset = [], 0
    while offset < len(payload):
        if len(found) >= MAX_PACKETS_PER_DATAGRAM:
            raise ValueError("saved RTCP datagram exceeds this path's compound packet bound")
        if offset + COMMON_HEADER > len(payload):
            raise ValueError("a saved RTCP datagram ends inside a packet header")
        first, packet_type, words = struct.unpack_from(">BBH", payload, offset)
        version, padding, count = first >> 6, (first >> 5) & 1, first & 0b11111
        if version != RTCP_VERSION:
            raise ValueError("a saved RTCP packet declares a version this path does not read")
        length = (words + 1) * 4
        if offset + length > len(payload):
            raise ValueError("a saved RTCP packet declares a length past the end of its datagram")
        body = payload[offset:offset + length]
        # The saved bytes and the bytes that carry meaning are kept apart. `body` stays exactly
        # what was recorded, so the digest binds it; `effective` is what the structure rules are
        # allowed to read. Letting padding satisfy a missing field would let a short report look
        # valid because somebody padded it.
        effective, padding_bytes = body, 0
        if padding:
            # §6.4.1: the last octet "is a count of how many padding octets should be ignored,
            # including itself (it will be a multiple of four)". Zero would ignore nothing, a
            # count past the packet would ignore bytes that are not there, and a count that is
            # not a multiple of four would leave the remainder off the 32-bit boundary every
            # RTCP structure in the pinned source is written on.
            if length < COMMON_HEADER + 1:
                raise ValueError("a saved RTCP packet declares padding it has no room for")
            padding_bytes = body[-1]
            if not 0 < padding_bytes <= length - COMMON_HEADER:
                raise ValueError("a saved RTCP packet declares a padding count it cannot hold")
            if padding_bytes % 4:
                raise ValueError("a saved RTCP packet declares a padding count that is not the "
                                 "multiple of four the pinned source states")
            effective = body[:length - padding_bytes]
        found.append(dict(packet_type=packet_type, count=count, words=words, padding=bool(padding),
                          padding_bytes=padding_bytes, body=body, effective=effective))
        offset += length
    return found


def sender_information(effective, blocks):
    """The 20 sender-information octets §6.4.1 says are "present in every sender report".

    Every length here is measured against the **effective** body - the packet without the
    padding it declared - so padding can never stand in for a field the sender did not write.
    Returns the fields plus the count of bytes past the declared reception report blocks, which
    §6.4.1 allows as a "profile-specific extension" and which this path does not decode.
    """
    fixed = COMMON_HEADER + 4 + SENDER_INFO
    if len(effective) < fixed:
        raise ValueError("a saved RTCP sender report is shorter than its own sender information")
    if len(effective) < fixed + blocks * REPORT_BLOCK:
        # The declared reception report count and the declared length disagree; neither is
        # guessed and no block is read from bytes the packet does not have.
        raise ValueError("a saved RTCP sender report declares more reception report blocks than "
                         "its declared length can hold")
    ssrc, = struct.unpack_from(">I", effective, COMMON_HEADER)
    seconds, fraction, media, packets, octets = struct.unpack_from(">IIIII", effective,
                                                                   COMMON_HEADER + 4)
    extension = len(effective) - fixed - blocks * REPORT_BLOCK
    return ssrc, seconds, fraction, media, packets, octets, extension


def ntp_form(seconds, fraction, declared_form):
    """What can honestly be said about the form of the reported timestamp.

    §6.4.1 gives exactly one thing that holds for any capture: "A sender that has no notion of
    wallclock or elapsed time MAY set the NTP timestamp to zero", so all-zero is an absence.

    The high bit is **not** a proof. The pinned sentence is bounded in time - "**Until the year
    2036**, relative and absolute timestamps will differ in the high bit" - and this converter
    has no era, no epoch and no time scope of its own. After that rollover an absolute timestamp
    carries a clear high bit, so calling a clear bit "relative" would mislabel it. The era is
    also never inferred from the capture clock: the recorder's clock says nothing about which
    form the sender wrote. The bit itself is reported, and the form stays unresolved unless the
    caller declares one from a source that states it.
    """
    if seconds == 0 and fraction == 0:
        return "NO_TIME_DECLARED"
    if declared_form is not None:
        return "CALLER_DECLARED_" + declared_form + "_FORM_NOT_PROVEN_BY_THE_HIGH_BIT"
    return "FORM_UNRESOLVED_THE_HIGH_BIT_ALONE_PROVES_NEITHER_ABSOLUTE_NOR_RELATIVE"


def ntp_text(seconds, fraction):
    """`seconds + fraction / 2**32`, exact in decimal, with no epoch and no time zone.

    The fraction is a binary fraction of one second, so the exact decimal is finite and short;
    a float would round what the sender wrote.
    """
    with decimal.localcontext() as context:
        context.prec = 40
        return decimal.Decimal(seconds) + decimal.Decimal(fraction) / decimal.Decimal(NTP_FRACTION)


def cname_items(effective, chunks):
    """Every SSRC/CSRC chunk's CNAME item, in order, with what was not interpreted beside it.

    Returns `(chunks, trailing)` where each chunk is
    `(ssrc, cname bytes or None, {item type: count} for every item this path did not interpret)`
    and `trailing` is the number of bytes of the effective body left after the declared chunks.
    A count alone would not tell a reader which record information was left undecoded, so the
    item types are counted under their own numbers and the packet digest keeps the bytes
    recoverable.

    §6.5: each chunk is an identifier followed by items, "Each chunk starts on a 32-bit
    boundary", an item is "an 8-bit type field, an 8-bit octet count … and the text itself", and
    the list "MUST be terminated by one or more null octets". Only item type 1, CNAME, is
    interpreted; §6.5 says the others "may be useful only for particular profiles", so they are
    stepped over by their own declared length and never read as text.
    """
    found, offset, body = [], COMMON_HEADER, effective
    for _ in range(chunks):
        if offset + 4 > len(body):
            raise ValueError("a saved RTCP source description ends inside a chunk identifier")
        identifier, = struct.unpack_from(">I", body, offset)
        offset += 4
        name, uninterpreted = None, {}
        while True:
            if offset >= len(body):
                raise ValueError("a saved RTCP source description chunk is not terminated")
            item = body[offset]
            if item == 0:
                # "No length octet follows the null item type octet, but additional null octets
                # MUST be included if needed to pad until the next 32-bit boundary."
                offset += 1
                while offset % 4:
                    if offset >= len(body) or body[offset] != 0:
                        raise ValueError("a saved RTCP source description chunk is padded with "
                                         "something other than the null octets the source requires")
                    offset += 1
                break
            if offset + 2 > len(body):
                raise ValueError("a saved RTCP source description ends inside an item header")
            size = body[offset + 1]
            if offset + 2 + size > len(body):
                raise ValueError("a saved RTCP source description item runs past its packet")
            if item == CNAME_ITEM:
                if name is not None:
                    raise ValueError("a saved RTCP source description chunk repeats its canonical "
                                     "name item, so neither is taken as the declared identity")
                name = body[offset + 2:offset + 2 + size]
            else:
                # Stepped over by its own declared length, and counted under its own type number
                # so the row and the report say which information was left undecoded.
                key = str(item)
                uninterpreted[key] = uninterpreted.get(key, 0) + 1
            offset += 2 + size
        found.append((identifier, name, uninterpreted))
    # §6.5 allows only chunks here, so anything after the declared ones is unaccounted rather
    # than ignored: it is counted and the packet digest keeps it recoverable.
    return found, len(body) - offset


def convert(raw, rtcp_port, declared_form=None):
    """One row per sender report and per declared canonical name, in saved capture order."""
    if not raw or len(raw) > LIMIT:
        raise ValueError("empty or oversized saved capture")
    if declared_form is not None and declared_form not in DECLARED_FORMS:
        raise ValueError("a declared sender time form must be one the pinned source names")
    # The port range is the shared walker's own rule and it raises on the first record; this
    # check only brings the same refusal forward for a caller that imports `convert` directly.
    if type(rtcp_port) is not int or not 1 <= rtcp_port <= 65535:
        raise ValueError("explicit UDP destination port required")
    records = read_pcap_records(raw)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    report = dict(reference=REFERENCE, source_sha256=digest, source_bytes=len(raw),
                  rtcp_port=rtcp_port, datagrams_selected=0, rtcp_packets_read=0,
                  sender_reports=0, canonical_names=0,
                  # Never a silent drop: every packet type this path does not interpret is
                  # counted under its own number, and the reception report blocks it refuses to
                  # decode are counted too.
                  packets_by_type={}, reception_report_blocks_counted=0,
                  # Item-level accounting, because a packet count alone does not tell a reader
                  # which record information was left undecoded.
                  uninterpreted_sdes_items_by_type={}, sdes_chunks_without_canonical_name=0,
                  sdes_unconsumed_trailing_bytes=0, sender_report_extension_bytes=0,
                  padding_bytes_excluded=0, declared_sender_time_form=declared_form,
                  sender_report_sources=[], time_basis=("SENDER_DECLARED_PAIRING_OF_ITS_OWN_"
                                                        "CLOCKS_NO_UTC_NO_ERA_AND_NO_RECEIVER_"
                                                        "ALIGNMENT"))
    sources, rows = [], 0
    for saved in iter_udp_datagrams(raw, rtcp_port, records):
        if saved.payload is None:
            continue
        report["datagrams_selected"] += 1
        fixed = {"record_time_us": saved.capture_time_us, "pcap_clock_basis": CAPTURE_BASIS,
                 "pcap_packet_index": saved.index, "pcap_source_sha256": digest,
                 "rtcp_source_address": saved.source_address,
                 "rtcp_source_port": saved.source_port,
                 "rtcp_destination_address": saved.destination_address,
                 "rtcp_destination_port": saved.destination_port,
                 "record_basis": RECORD_BASIS}
        for position, packet in enumerate(compound_packets(saved.payload)):
            report["rtcp_packets_read"] += 1
            name = str(packet["packet_type"])
            report["packets_by_type"][name] = report["packets_by_type"].get(name, 0) + 1
            shared = dict(fixed, rtcp_packet_index_in_datagram=position,
                          rtcp_packet_type=packet["packet_type"],
                          rtcp_packet_type_name=NAMED_TYPES.get(packet["packet_type"],
                                                                "TYPE_RETAINED_NOT_INTERPRETED"),
                          rtcp_length_words=packet["words"],
                          rtcp_padding_declared=int(packet["padding"]),
                          rtcp_padding_bytes_excluded=packet["padding_bytes"],
                          # The digest is over the packet exactly as it was saved, padding
                          # included, so the bytes the structure rules did not read stay
                          # recoverable from the retained original.
                          rtcp_packet_sha256="sha256:" + hashlib.sha256(
                              packet["body"]).hexdigest())
            report["padding_bytes_excluded"] += packet["padding_bytes"]
            if packet["packet_type"] == SENDER_REPORT:
                blocks = packet["count"]
                (ssrc, seconds, fraction, media, packets, octets,
                 extension) = sender_information(packet["effective"], blocks)
                report["reception_report_blocks_counted"] += blocks
                report["sender_report_extension_bytes"] += extension
                form = ntp_form(seconds, fraction, declared_form)
                declared = form != "NO_TIME_DECLARED"
                writer.writerow(dict(
                    shared, rtcp_record_kind="SENDER_REPORT", rtcp_ssrc=ssrc,
                    sr_ntp_seconds_reported=seconds, sr_ntp_fraction_reported=fraction,
                    sr_ntp_seconds_text=(ntp_text(seconds, fraction) if declared else None),
                    # The bit is preserved as its own reported value; it decides nothing here.
                    sr_ntp_high_bit_reported=int(bool(seconds & NTP_HIGH_BIT)),
                    sr_ntp_form=form, sr_ntp_form_basis=FORM_BASIS, sr_ntp_basis=NTP_BASIS,
                    sr_rtp_timestamp_reported=media,
                    # The pairing exists only if the sender declared a time at all. It pairs the
                    # sender's RTP clock with the time it reported, whatever form that is.
                    sr_media_sender_clock_pairing=(
                        "SENDER_DECLARED_PAIRING_OF_ITS_OWN_RTP_CLOCK_AND_ITS_REPORTED_TIME"
                        if declared else "NO_PAIRING_DECLARED_THE_SENDER_WROTE_NO_TIME"),
                    sr_pairing_basis=PAIRING_BASIS,
                    sr_sender_packet_count_reported=packets,
                    sr_sender_octet_count_reported=octets, sr_sender_count_basis=COUNT_BASIS,
                    sr_reception_report_blocks_declared=blocks,
                    sr_profile_extension_bytes=extension,
                    sr_reception_report_basis=BLOCK_BASIS))
                report["sender_reports"] += 1
                if ssrc not in sources:
                    sources.append(ssrc)
                rows += 1
            elif packet["packet_type"] == SOURCE_DESCRIPTION:
                chunks, trailing = cname_items(packet["effective"], packet["count"])
                report["sdes_unconsumed_trailing_bytes"] += trailing
                for identifier, cname, uninterpreted in chunks:
                    for item_type, count in uninterpreted.items():
                        report["uninterpreted_sdes_items_by_type"][item_type] = (
                            report["uninterpreted_sdes_items_by_type"].get(item_type, 0) + count)
                    if cname is None:
                        report["sdes_chunks_without_canonical_name"] += 1
                        continue
                    if len(cname) > MAX_CNAME_BYTES:
                        raise ValueError("a saved canonical name is longer than the pinned "
                                         "source allows")
                    writer.writerow(dict(
                        shared, rtcp_record_kind="SOURCE_DESCRIPTION_CNAME", rtcp_ssrc=identifier,
                        sdes_cname_hex="hex:" + cname.hex(), sdes_cname_basis=CNAME_BASIS,
                        sdes_uninterpreted_items_in_chunk=sum(uninterpreted.values()),
                        sdes_unconsumed_trailing_bytes=trailing))
                    report["canonical_names"] += 1
                    rows += 1
            if rows > MAX_ROWS:
                raise ValueError("saved capture exceeds this path's row bound")
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("converted RTCP output exceeds bound")
    if not report["rtcp_packets_read"]:
        raise ValueError("no RTCP packet on the declared port in the saved capture")
    report["sender_report_sources"] = sources
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved PCAP capture already on disk")
    parser.add_argument("output_directory", type=Path, help="new directory for the CSV and report")
    parser.add_argument("--rtcp-port", type=int, required=True,
                        help="the UDP port the RTCP packets were captured on; declared by the "
                             "caller from the session description, never inferred from the RTP "
                             "port because 'port plus one' is a convention and not a rule")
    parser.add_argument("--declared-sender-time-form", choices=DECLARED_FORMS, default=None,
                        help="optional: the form the PRODUCER'S OWN documentation states its "
                             "sender reports carry. Without it the form stays unresolved, "
                             "because the pinned source separates the two forms by the high bit "
                             "only until the year 2036 and this path has no era of its own")
    args = parser.parse_args(argv)
    with args.input.open("rb") as handle:
        raw = handle.read(LIMIT + 1)
    converted, report = convert(raw, args.rtcp_port, args.declared_sender_time_form)
    args.output_directory.mkdir()
    (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
    (args.output_directory / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
