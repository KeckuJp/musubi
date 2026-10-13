"""Passive classic CAN export with caller-qualified DBC; no CAN device access."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from scripts.convert_dronecan_capture import capture_rows
from scripts.recorded_units import UNITS, normalize
OUTPUT_LIMIT = 128 * 1024 * 1024
FIELDS = ["record_time_us", "frame_id", "frame_kind", "dbc_sha256",
          "signal_name_hex", "source_data_hex"] + sorted({v[0] for v in UNITS.values()})


# Opt-in saved J1939 meaning over the same strict decoder (cantools 40.7.1 j1939.py PGN rules;
# Open-SAE-J1939 4360e08b transport and Address Claim NAME layout). Declarations, not identity.
J1939_FIELDS = ["j1939_pgn", "j1939_priority", "j1939_source_address", "j1939_destination_address",
                "j1939_match_basis", "j1939_dbc_declared_source_address", "j1939_transfer_basis",
                "j1939_transfer_index", "j1939_transfer_sha256", "j1939_transfer_first_time_us",
                "j1939_reported_name_hex", "j1939_name_association_status", "j1939_claim_kind",
                "j1939_name_identity_number", "j1939_name_manufacturer_code", "j1939_name_ecu_instance",
                "j1939_name_function_instance", "j1939_name_function", "j1939_name_vehicle_system",
                "j1939_name_vehicle_system_instance", "j1939_name_industry_group",
                "j1939_name_arbitrary_address_capable"]
J1939_NETWORK_PGNS = {59904: "REQUEST", 60928: "ADDRESS_CLAIM", 60416: "TP_CM", 60160: "TP_DT"}
J1939_MAX_TRANSFER = 1785
J1939_REPORT_LIMIT = 16 * 1024 * 1024
# The pinned writer (Address_Claimed.c / Address_Not_Claimed.c) sends only 0x18EEFF<SA>:
# priority 6, PGN 60928 to the global address, from SA 0..253 or 254 (Cannot Claim).
J1939_CLAIM_FORM = 0x18eeff00


def name_fields(payload):
    """Pinned Address_Claimed.c byte layout; a producer report, never authenticated identity."""
    return dict(j1939_name_identity_number=((payload[2] & 0x1f) << 16) | (payload[1] << 8) | payload[0],
                j1939_name_manufacturer_code=(payload[3] << 3) | (payload[2] >> 5),
                j1939_name_ecu_instance=payload[4] & 7, j1939_name_function_instance=payload[4] >> 3,
                j1939_name_function=payload[5], j1939_name_vehicle_system=payload[6] >> 1,
                j1939_name_vehicle_system_instance=payload[7] & 15,
                j1939_name_industry_group=(payload[7] >> 4) & 7,
                j1939_name_arbitrary_address_capable=payload[7] >> 7)


class NameEpochs:
    """Address/NAME association within one supplied capture; no arbitration winner or backfill."""

    def __init__(self):
        self.by_address, self.owner, self.version = {}, {}, {}

    def _set(self, address, **state):
        self.by_address[address] = state
        self.version[address] = self.version.get(address, 0) + 1

    def claim(self, timestamp, address, name):
        if address == 254:  # Cannot Claim: this NAME reports holding no address.
            old = self.owner.pop(name, None)
            if old is not None and self.by_address.get(old, {}).get("name") == name:
                self._set(old, name=None, status="NAME_MOVED_OR_RELEASED", since=timestamp)
            return "CANNOT_CLAIM_REPORTED"
        current = self.by_address.get(address)
        old = self.owner.get(name)
        if old is not None and old != address and self.by_address.get(old, {}).get("name") == name:
            self._set(old, name=None, status="NAME_MOVED_TO_OTHER_ADDRESS", since=timestamp)
        self.owner[name] = address
        if current is not None and current["status"].startswith("CONTESTED"):
            # No pinned arbitration rule: a repeated contender or another NAME never resolves it.
            self._set(address, name=None, status=current["status"], since=timestamp)
        elif current is None:
            self._set(address, name=name, status="CLAIMED_EARLIER_IN_THIS_CAPTURE", since=timestamp)
        elif current["name"] is None:
            # Only after an observed move away / Cannot Claim by the previous NAME.
            self._set(address, name=name, status="ADDRESS_REASSIGNED_IN_CAPTURE", since=timestamp)
        elif current["name"] != name:
            previous_still_here = self.owner.get(current["name"]) == address
            if previous_still_here or current["since"] == timestamp:
                self._set(address, name=None, status="CONTESTED_NO_ARBITRATION_INFERRED", since=timestamp)
            else:
                self._set(address, name=name, status="ADDRESS_REASSIGNED_IN_CAPTURE", since=timestamp)
        return self.by_address[address]["status"]

    def association(self, timestamp, address):
        state = self.by_address.get(address)
        if state is None:
            return "", "NO_PRIOR_CLAIM_IN_CAPTURE"
        if state["since"] == timestamp:
            return "", "SAME_TIME_AS_CLAIM_ORDER_UNKNOWN"
        return ("hex:" + state["name"].to_bytes(8, "little").hex() if state["name"] is not None else ""), state["status"]


def convert(text, dbc, *, extended=False, include_enums=False, fd=False, j1939=False):
    import cantools
    if cantools.__version__ != "40.7.1":
        raise ValueError("unqualified decoder version")
    if type(j1939) is not bool or (j1939 and (not extended or fd)):
        raise ValueError("J1939 meaning requires explicit classic extended frames")
    if not isinstance(dbc, bytes) or len(dbc) > 2 * 1024 * 1024:
        raise ValueError("expected bounded DBC bytes")
    database = cantools.database.load_string(dbc.decode("utf-8"), database_format="dbc", strict=True)
    digest = hashlib.sha256(dbc).hexdigest()
    messages = {}
    for message in database.messages:
        key = (message.frame_id, message.is_extended_frame)
        if key in messages:
            raise ValueError("ambiguous DBC identifier")
        messages[key] = message
    by_pgn, multipacket = {}, {}
    if j1939:
        from cantools.j1939 import pgn_from_frame_id
        for message in database.messages:
            if not message.is_extended_frame or message.is_fd or message.is_container:
                continue
            pgn, pdu_format = pgn_from_frame_id(message.frame_id), (message.frame_id >> 16) & 255
            if pgn in J1939_NETWORK_PGNS:
                raise ValueError("DBC declares a J1939 network-management or transport PGN")
            if not 0 < message.length <= J1939_MAX_TRANSFER:
                raise ValueError("DBC message length outside the classic J1939 transfer bound")
            if pdu_format in (239, 255):
                if message.length > 8:
                    raise ValueError("proprietary multi-packet PGN is not qualified")
                continue  # Proprietary meaning is per manufacturer: exact identifier only.
            if pgn in by_pgn:
                raise ValueError("ambiguous DBC PGN")
            by_pgn[pgn] = message
            if message.length > 8:
                multipacket[pgn] = message
    output = io.StringIO(newline="")
    fields = FIELDS + (["enum_raw_code", "enum_label_hex", "enum_status", "multiplex_selector_reported"] if include_enums else [])
    if fd:
        fields += ["fd_flags_reported"]
    if j1939:
        fields += J1939_FIELDS
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    report = dict(frames=0, decoded_frames=0, observations=0, unsupported=[],
                  dbc_sha256=digest, clock="Unknown", decoder="cantools-40.7.1")
    retained_size = 0

    def unsupported(record):
        nonlocal retained_size
        retained_size += len(json.dumps(record))
        if retained_size > 16 * 1024 * 1024:
            raise ValueError("unsupported report exceeds bound")
        report["unsupported"].append(record)

    def decode(line, row, timestamp, identifier, data, message, extra):
        if len(data) != message.length:
            raise ValueError("DBC frame length mismatch")
        values = message.decode(data, decode_choices=False, scaling=not include_enums,
                                allow_truncated=False, allow_excess=False)
        emitted = 0
        for signal in message.signals:
            if signal.name not in values:  # Inactive multiplex branch is not a missing observation.
                continue
            base = dict(record_time_us=timestamp, frame_id=identifier,
                frame_kind=("extended" if extended else "standard") + ("_fd" if fd else ""), dbc_sha256="sha256:" + digest,
                signal_name_hex="hex:" + signal.name.encode().hex(), source_data_hex="hex:" + data.hex(), **extra)
            if fd:
                base["fd_flags_reported"] = int(row[2], 16)
            if include_enums and (signal.choices or signal.is_multiplexer):
                raw = values[signal.name]
                if type(raw) is not int or not -(2**63) <= raw < 2**63:
                    raise ValueError("enum requires common-range raw integer")
                label = signal.choices.get(raw) if signal.choices else None
                writer.writerow(dict(base, enum_raw_code=raw,
                    enum_label_hex="hex:" + str(label).encode().hex() if label is not None else "",
                    enum_status="DECLARED_LABEL" if label is not None else "UNKNOWN_CODE" if signal.choices else "SELECTOR_NO_LABEL",
                    multiplex_selector_reported=int(signal.is_multiplexer)))
                emitted += 1
                continue
            unit = UNITS.get(signal.unit)
            if unit is None or signal.choices or signal.is_multiplexer:
                unsupported(dict(line=line, source=row,
                    signal=signal.name, reason="UNIT_OR_ENUM_NOT_QUALIFIED"))
                continue
            value = (signal.conversion.raw_to_scaled(values[signal.name], decode_choices=False)
                     if include_enums else values[signal.name])
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 2**53:
                raise ValueError("unrepresentable decoded numeric signal")
            field, normalized = normalize(value, signal.unit)
            writer.writerow(dict(base, **{field: normalized}))
            emitted += 1
        report["observations"] += emitted
        report["decoded_frames"] += int(emitted > 0)
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("expanded output exceeds bound")

    if j1939:
        convert_j1939_frames(capture_rows(text, extended=True), messages, by_pgn, multipacket,
                             report, unsupported, decode, writer, digest, output)
        report["conversion_status"] = "OBSERVATIONS_PRODUCED" if report["observations"] else "NO_SUPPORTED_OBSERVATIONS"
        if len(json.dumps(report).encode()) > J1939_REPORT_LIMIT:
            raise ValueError("complete J1939 report exceeds bound")
    else:
        for line, row, timestamp, identifier, data in capture_rows(text, extended=extended, fd=fd):
            report["frames"] += 1
            message = messages.get((identifier, extended))
            if message is not None and fd and not message.is_fd:
                raise ValueError("FD capture requires explicit FD dictionary declaration")
            if message is None or (message.is_fd and not fd) or message.is_container:
                unsupported(dict(line=line, source=row, reason="UNSELECTED_FRAME"))
                continue
            decode(line, row, timestamp, identifier, data, message, {})
    report["conversion_status"] = "OBSERVATIONS_PRODUCED" if report["observations"] else "NO_SUPPORTED_OBSERVATIONS"
    return output.getvalue(), report


def convert_j1939_frames(frames, messages, by_pgn, multipacket, report, unsupported, decode, writer, digest, output):
    """PGN-matched single frames, declared fixed-length BAM/RTS transfers and Address Claims."""
    from cantools.j1939 import pgn_from_frame_id
    from scripts.convert_dronecan_capture import _bam_step, _RtsTransfer
    epochs, bam_pending, pairs, ignored, starts = NameEpochs(), {}, {}, set(), {}
    report.update(completed_transfers=0, j1939=dict(
        basis="PGN_MATCH_CANTOOLS_40_7_1_PDU1_DESTINATION_EXCLUDED_PROPRIETARY_EXACT_ID_ONLY",
        name_basis="ADDRESS_CLAIM_NAME_IS_A_PRODUCER_REPORT_WITHIN_THIS_CAPTURE_NO_ARBITRATION_OR_BACKFILL",
        transfers=[], claims=[], declared_multipacket_pgns=sorted(multipacket)))
    selected = lambda target: target in multipacket
    bam_valid = lambda target, size, count, reserved: (
        size == multipacket[target].length and count == (size + 6) // 7 and reserved == 255)
    rts_valid = lambda target, size: size == multipacket[target].length
    evidence_bytes = 0

    def emit(line, row, timestamp, identifier, data, message, basis, transfer=None):
        """`identifier` is the captured frame that completed this observation; for a transfer the
        sender, receiver and transported PGN come from the reconstructed exchange instead."""
        nonlocal evidence_bytes
        pdu_format, pdu_specific = (identifier >> 16) & 255, (identifier >> 8) & 255
        source = identifier & 255 if transfer is None else transfer["source"]
        name, status = epochs.association(timestamp, source)
        if transfer is not None:
            # The payload belongs to the exchange, so its sender's NAME must hold over the whole
            # interval; a change between announcement and completion is withheld, never relabelled.
            version, (start_name, start_status) = starts.pop(transfer["key"])
            start = (start_status, start_name)
            completion = (status, name)
            if epochs.version.get(source, 0) != version or start != completion:
                # Both ends must justify the same NAME (or the same absence); an uncertain start,
                # such as a same-time claim at the announcement, is never resolved by completion.
                name, status = "", "NAME_ASSOCIATION_NOT_CONSTANT_OVER_TRANSFER_WITHHELD"
        extra = dict(j1939_pgn=pgn_from_frame_id(message.frame_id), j1939_priority=identifier >> 26,
                     j1939_source_address=source,
                     j1939_destination_address=pdu_specific if pdu_format < 240 else "",
                     j1939_match_basis=basis, j1939_dbc_declared_source_address=message.frame_id & 255,
                     j1939_transfer_basis="SINGLE_FRAME", j1939_reported_name_hex=name,
                     j1939_name_association_status=status)
        if transfer is not None:
            retained = json.dumps(transfer["rows"], separators=(",", ":")).encode()
            index = len(report["j1939"]["transfers"])
            sha = "sha256:" + hashlib.sha256(retained).hexdigest()
            evidence_bytes += len(retained) * 2
            if evidence_bytes > 16 * 1024 * 1024:
                raise ValueError("J1939 transfer evidence exceeds bound")
            report["j1939"]["transfers"].append(dict(transfer_index=index, transfer_sha256=sha,
                source_frames_hex="hex:" + retained.hex(), basis=transfer["basis"],
                first_time_us=transfer["first"], completion_line=line,
                start_name_association=list(start), completion_name_association=list(completion)))
            extra.update(j1939_destination_address=transfer["destination"], j1939_transfer_basis=transfer["basis"],
                         j1939_transfer_index=index, j1939_transfer_sha256=sha,
                         j1939_transfer_first_time_us=transfer["first"])
        decode(line, row, timestamp, identifier, data, message, extra)

    for line, row, timestamp, identifier, data in frames:
        report["frames"] += 1
        pdu_format, pdu_specific, source = (identifier >> 16) & 255, (identifier >> 8) & 255, identifier & 255
        pgn = pgn_from_frame_id(identifier)
        if pgn == 60928:
            if identifier & 0x1fffff00 != J1939_CLAIM_FORM or source == 255:
                unsupported(dict(line=line, source=row, reason="UNQUALIFIED_ADDRESS_CLAIM_FORM"))
                continue
            if len(data) != 8:
                raise ValueError("address claim must carry an 8-byte NAME")
            name = int.from_bytes(data, "little")
            status = epochs.claim(timestamp, source, name)
            kind = "CANNOT_CLAIM" if source == 254 else "ADDRESS_CLAIMED"
            if len(report["j1939"]["claims"]) >= 65536:
                raise ValueError("address claim accounting exceeds bound")
            report["j1939"]["claims"].append(dict(line=line, time_us=timestamp, source_address=source,
                                                  name_hex=data.hex(), kind=kind, status_after=status))
            writer.writerow(dict(record_time_us=timestamp, frame_id=identifier, frame_kind="extended",
                dbc_sha256="sha256:" + digest, source_data_hex="hex:" + data.hex(), j1939_pgn=pgn,
                j1939_priority=identifier >> 26, j1939_source_address=source,
                j1939_destination_address=pdu_specific, j1939_match_basis="FIXED_ADDRESS_CLAIM_LAYOUT_4360E08B",
                j1939_reported_name_hex="hex:" + data.hex(), j1939_name_association_status=status,
                j1939_claim_kind=kind, **name_fields(data)))
            report["decoded_frames"] += 1
            report["observations"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("expanded output exceeds bound")
            continue
        if source > 253:
            # The adopted range: ordinary senders are 0..253; 254 is only the Cannot Claim exception
            # above and 255 is a destination, never a sender.
            unsupported(dict(line=line, source=row, reason="UNQUALIFIED_SOURCE_ADDRESS"))
            continue
        if pdu_format in (0xec, 0xeb):
            if pdu_specific != 0xff and (pdu_specific > 253 or pdu_specific == source):
                unsupported(dict(line=line, source=row, reason="UNQUALIFIED_ADDRESSED_PAIR"))
                continue
            if pdu_specific == 0xff:
                step = _bam_step(bam_pending, row, timestamp, identifier, data, selected, bam_valid,
                                 "invalid declared multi-packet BAM")
                if step == "consumed" and pdu_format == 0xec:
                    starts[("bam", source)] = (epochs.version.get(source, 0), epochs.association(timestamp, source))
                if isinstance(step, dict):
                    # Selected completed transfers, counted once here; RTS ones are counted by the
                    # shared step, and the DM1/DM2 BAM report keeps its previous keys.
                    report["completed_transfers"] += 1
                    message = multipacket[step["pgn"]]
                    emit(line, row, timestamp, identifier, bytes(step["data"][:step["size"]]), message,
                         "PGN_ANY_SOURCE_DBC_SA_IGNORED", dict(rows=step["rows"], basis="BAM", source=step["source"],
                                                               destination=255, first=step["first"],
                                                               key=("bam", step["source"])))
                elif step != "consumed":
                    unsupported(dict(line=line, source=row, reason="UNSELECTED_OR_ORPHAN_BROADCAST_TRANSPORT"))
                continue
            control = pdu_format == 0xec
            code = data[0] if control and data else None
            key = (pdu_specific, source) if code in (17, 19) else (source, pdu_specific)
            if code == 255:  # Abort may come from either side.
                key = next((k for k in ((source, pdu_specific), (pdu_specific, source))
                            if k in ignored or (k in pairs and pairs[k].pending)), key)
            if key in ignored:
                unsupported(dict(line=line, source=row, reason="UNSELECTED_ADDRESSED_TRANSPORT"))
                if code in (19, 255):
                    ignored.discard(key)
                continue
            if control and len(data) == 8 and not selected(int.from_bytes(data[5:8], "little")):
                if not (key in pairs and pairs[key].pending):
                    if code == 16:
                        ignored.add(key)
                    unsupported(dict(line=line, source=row, reason="UNSELECTED_ADDRESSED_TRANSPORT"))
                    continue
            if not control and not (key in pairs and pairs[key].pending):
                unsupported(dict(line=line, source=row, reason="ORPHAN_TRANSPORT_DATA_PGN_UNKNOWN"))
                continue
            transfer = pairs.setdefault(key, _RtsTransfer(key[0], key[1], selected, rts_valid))
            result = transfer.step(row, timestamp, identifier, data, report)
            if result == "consumed" and code == 16:
                starts[("rts", key)] = (epochs.version.get(key[0], 0), epochs.association(timestamp, key[0]))
            if isinstance(result, tuple):
                message = multipacket[result[3]]
                emit(line, row, timestamp, identifier, result[4], message, "PGN_ANY_SOURCE_DBC_SA_IGNORED",
                     dict(rows=result[5], basis=result[6]["basis"], source=key[0], destination=key[1],
                          first=result[1], key=("rts", key)))
            continue
        if pdu_format in (239, 255):
            message, basis = messages.get((identifier, True)), "EXACT_ID_PROPRIETARY"
        else:
            message, basis = by_pgn.get(pgn), "PGN_ANY_SOURCE_DBC_SA_IGNORED"
        if message is None or message.is_fd or message.is_container or message.length > 8:
            unsupported(dict(line=line, source=row, reason="UNSELECTED_FRAME" if message is None or message.length <= 8
                             else "MULTI_PACKET_PGN_SENT_AS_SINGLE_FRAME"))
            continue
        emit(line, row, timestamp, identifier, data, message, basis)
    if bam_pending or any(transfer.pending for transfer in pairs.values()):
        raise ValueError("incomplete declared multi-packet transfer")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("dbc", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--include-enums", action="store_true", help="retain raw choice codes and declared labels, not physical-state certification")
    parser.add_argument("--fd", action="store_true", help="require explicit saved FD capture and matching DBC frame declaration")
    parser.add_argument("--j1939", action="store_true", help="PGN-matched saved J1939 meaning, declared fixed-length transfers and reported Address Claim NAME")
    args = parser.parse_args()
    with args.capture.open("rb") as source:
        capture = source.read(16 * 1024 * 1024 + 1).decode("utf-8")
    with args.dbc.open("rb") as source:
        dbc = source.read(2 * 1024 * 1024 + 1)
    output, report = convert(capture, dbc, extended=args.extended, include_enums=args.include_enums, fd=args.fd,
                             j1939=args.j1939)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(output)
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    if not report["observations"]:
        parser.exit(2, "No supported observations; diagnostic report saved, not a successful adaptation\n")


if __name__ == "__main__":
    main()
