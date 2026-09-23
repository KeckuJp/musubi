#!/usr/bin/env python3
"""Saved ExpressLRS configuration export; no device, link or transmission is involved.

The producer is GetConfiguration in src/lib/WIFI/devWIFI.cpp at ExpressLRS 3.6.4
(b61c9e24305b2f80046a5e0b3c4edf56c4f059a3), serving /config with an optional
"export" argument. Reading a saved settings document reports what was configured;
it does not change a device and says nothing about whether a link works.

Scope is the transmitter export shape only. The caller must declare the exact source
format, because the exported bytes carry no version field: that declaration is a
precondition the operator asserts, never in-band firmware authentication, and the
version is deliberately not guessed from the document shape.

The same producer can emit wifi-ssid, wifi-password, uid and flash-discriminator,
and config.uid is the binding credential in both modes, so nothing here copies the
whole document. Selected settings are emitted by name and every input key is
accounted for, but a redacted key yields only a fixed reason, its path and a member
count: no digest of a secret value, because a short credential is guessable from its
hash, and no member key names. An unknown value is never inlined at all, whatever its
JSON type: a number can be a PIN, a one-time code or a numeric binding identifier
just as a string can, so it is reference-only against the retained original. Only the
source-qualified whitelisted settings below reach a common row, after passing their
own bitfield and enum validation.
"""
import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path

try:
    from scripts.convert_ardupilot_battery_csv import unique_object
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import unique_object

PIN = "ExpressLRS/ExpressLRS@b61c9e24305b2f80046a5e0b3c4edf56c4f059a3"
# The export carries no version field, so this is a caller precondition, not
# authentication of the firmware that produced the document.
SOURCE_FORMAT = "elrs-config-export-3.6.4"
SOURCE_FORMAT_BASIS = "CALLER_DECLARED_PRECONDITION_NOT_IN_BAND_FIRMWARE_AUTHENTICATION"
SCOPE = "transmitter export shape only; the non-export /config form and receiver exports are out of scope"
REDACTION_REASON = "CREDENTIAL_OR_IDENTIFIER_KEY_NOT_EMITTED"
REFERENCE_ONLY = "REFERENCE_ONLY_SEE_SOURCE_DOCUMENT_AT_THIS_PATH"
LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
MODEL_COUNT = 64  # config.h CONFIG_TX_MODEL_CNT
COLUMNS = ("record_time_us", "elrs_setting_index", "elrs_scope", "elrs_model_index",
           "elrs_setting_path_hex", "elrs_value_reported", "elrs_value_label",
           "elrs_value_status", "packet_rate_configured_hz", "packet_rate_basis",
           "telemetry_ratio_denominator", "elrs_source_sha256")
# common.cpp ExpressLRS_AirRateConfig, defined once per radio; the export does not say
# which radio it came from, so the caller must declare it or the index stays unresolved.
RATE_TABLES = {
    "sx127x": ("RATE_LORA_200HZ", "RATE_LORA_100HZ_8CH", "RATE_LORA_100HZ",
               "RATE_LORA_50HZ", "RATE_LORA_25HZ", "RATE_DVDA_50HZ"),
    "sx128x": ("RATE_FLRC_1000HZ", "RATE_FLRC_500HZ", "RATE_DVDA_500HZ", "RATE_DVDA_250HZ",
               "RATE_LORA_500HZ", "RATE_LORA_333HZ_8CH", "RATE_LORA_250HZ",
               "RATE_LORA_150HZ", "RATE_LORA_100HZ_8CH", "RATE_LORA_50HZ"),
    "lr1121": ("RATE_LORA_200HZ", "RATE_LORA_100HZ_8CH", "RATE_LORA_100HZ", "RATE_LORA_50HZ",
               "RATE_LORA_500HZ", "RATE_LORA_333HZ_8CH", "RATE_LORA_250HZ", "RATE_LORA_150HZ",
               "RATE_LORA_100HZ_8CH", "RATE_LORA_50HZ", "RATE_LORA_150HZ",
               "RATE_LORA_100HZ_8CH", "RATE_LORA_250HZ", "RATE_LORA_200HZ_8CH",
               "RATE_FSK_2G4_DVDA_500HZ", "RATE_FSK_900_1000HZ_8CH")}
RATE_BASIS = "CONFIGURED_TABLE_LABEL_FOR_DECLARED_RADIO_NOT_OBSERVED_THROUGHPUT"
RATE_UNRESOLVED = "PACKET_RATE_UNRESOLVED_RADIO_NOT_DECLARED"
# common.h expresslrs_tlm_ratio_e; the denominator is one telemetry response per N packets.
TLM_LABELS = ("TLM_RATIO_STD", "TLM_RATIO_NO_TLM", "TLM_RATIO_1_128", "TLM_RATIO_1_64",
              "TLM_RATIO_1_32", "TLM_RATIO_1_16", "TLM_RATIO_1_8", "TLM_RATIO_1_4",
              "TLM_RATIO_1_2", "TLM_RATIO_DISARMED")
TLM_DENOMINATORS = {2: 128, 3: 64, 4: 32, 5: 16, 6: 8, 7: 4, 8: 2}
SWITCH_LABELS = ("smWideOr8ch", "smHybridOr16ch", "sm12ch")  # OTA.h OtaSwitchMode_e
# model_config_t bitfield widths in config.h give the exact storable range per field.
MODEL_FIELDS = (("packet-rate", 4), ("telemetry-ratio", 4), ("switch-mode", 2),
                ("model-match", 1), ("tx-antenna", 2))
MODEL_POWER_FIELDS = (("max-power", 3), ("dynamic-power", 1), ("boost-channel", 3))
DEVICE_FIELDS = (("fan-mode", 8), ("power-fan-threshold", 8), ("motion-mode", 8))
NESTED_DEVICE_FIELDS = (("vtx-admin", ("band", "channel", "pitmode", "power")),
                        ("backpack", ("dvr-start-delay", "dvr-stop-delay", "dvr-aux-channel")))
# Each nested setting states its own kind, taken from the stored struct comments and one consumer at
# this same pin -- not from the key's name, and not from the fact that the export carries an integer:
#   src/lib/CONFIG/config.h:85-88   vtxBand    "0=Off, else band number"
#                                   vtxChannel "0=Ch1 -> 7=Ch8"
#                                   vtxPower   "0=Do not set, else power number"
#                                   vtxPitmode "Off/On/AUX1^/AUX1v/etc"
#                          :93-97   dvrStopDelay:3, dvrStartDelay:3, dvrAux:5
#   src/lib/VTX/devVTX.cpp          PITMODE_OFF 0 / PITMODE_ON 1; `<= PITMODE_ON` is used directly,
#                                   otherwise auxNumber = pitmode/2 + 3 and auxInverted = pitmode%2;
#                                   vtxIdx = (band-1)*8 + channel; power is added to the frame as the
#                                   stored number and omitted entirely when zero
#   src/lib/Backpack/devBackpack.cpp GetDvrDelaySeconds indexes {0,5,15,30,45,60,120} seconds and falls
#                                   back to index 0 out of range; dvrAux 0 is off, else
#                                   auxNumber = (dvrAux-1)/2 + 4 and auxInverted = (dvrAux+1)%2
# So pitmode and dvrAux are ENCODED SELECTIONS, not flags or plain channel numbers, and the DVR delays
# are indices into a seconds table. Nothing is resolved here: no index is expanded, no frequency and no
# milliwatt value is produced.
VTX_BAND_STATUS = ("REPORTED_STORED_VTX_BAND_NUMBER_WHERE_ZERO_MEANS_OFF_AT_THIS_PIN_AND_THE_CONSUMER_"
                   "COMBINES_IT_WITH_THE_CHANNEL_AS_BAND_MINUS_ONE_TIMES_EIGHT_PLUS_CHANNEL_SO_NO_FREQUENCY_"
                   "IN_MHZ_FOLLOWS_FROM_IT_AND_NONE_IS_RESOLVED_HERE")
VTX_CHANNEL_STATUS = ("REPORTED_STORED_VTX_CHANNEL_ORDINAL_WHERE_ZERO_IS_CHANNEL_ONE_AND_SEVEN_IS_CHANNEL_"
                      "EIGHT_AT_THIS_PIN_WHICH_IS_A_POSITION_WITHIN_A_BAND_AND_STILL_NOT_A_FREQUENCY")
VTX_POWER_STATUS = ("REPORTED_STORED_VTX_POWER_NUMBER_WHERE_ZERO_MEANS_DO_NOT_SET_AT_THIS_PIN_AND_THE_CONSUMER_"
                    "PASSES_THE_NUMBER_THROUGH_UNCONVERTED_SO_NO_POWER_IN_MILLIWATTS_FOLLOWS_FROM_IT")
VTX_PITMODE_STATUS = ("REPORTED_STORED_VTX_PITMODE_SELECTION_WHICH_IS_NOT_A_BINARY_FLAG_ZERO_IS_OFF_AND_ONE_IS_"
                      "ON_WHILE_ANY_HIGHER_VALUE_ENCODES_AN_AUX_CHANNEL_AND_INVERSION_AS_AUX_NUMBER_PITMODE_"
                      "DIVIDED_BY_TWO_PLUS_THREE_WITH_INVERSION_PITMODE_MODULO_TWO_AT_THIS_PIN_AND_IT_IS_NEVER_"
                      "A_MEASUREMENT_OF_RADIATED_OUTPUT")
DVR_DELAY_STATUS = ("REPORTED_STORED_DVR_DELAY_TABLE_INDEX_WHOSE_PINNED_CONSUMER_MAPS_IT_TO_SECONDS_THROUGH_THE_"
                    "TABLE_0_5_15_30_45_60_120_FALLING_BACK_TO_INDEX_ZERO_WHEN_OUT_OF_RANGE_AND_IT_IS_NOT_"
                    "RESOLVED_TO_SECONDS_HERE_SO_THE_VALUE_IS_AN_INDEX_AND_NOT_A_DURATION")
DVR_AUX_STATUS = ("REPORTED_STORED_DVR_AUX_SELECTION_WHICH_IS_NOT_A_PLAIN_CHANNEL_NUMBER_ZERO_MEANS_OFF_WHILE_A_"
                  "NONZERO_VALUE_ENCODES_AN_AUX_CHANNEL_AND_INVERSION_AS_AUX_NUMBER_VALUE_MINUS_ONE_DIVIDED_BY_"
                  "TWO_PLUS_FOUR_WITH_INVERSION_VALUE_PLUS_ONE_MODULO_TWO_AT_THIS_PIN_AND_IT_IS_NEVER_A_COMMAND_"
                  "A_RECORDING_OR_EVIDENCE_THAT_A_RECORDER_RAN")
NESTED_FIELD_STATUS = {
    ("vtx-admin", "band"): VTX_BAND_STATUS, ("vtx-admin", "channel"): VTX_CHANNEL_STATUS,
    ("vtx-admin", "power"): VTX_POWER_STATUS, ("vtx-admin", "pitmode"): VTX_PITMODE_STATUS,
    ("backpack", "dvr-start-delay"): DVR_DELAY_STATUS,
    ("backpack", "dvr-stop-delay"): DVR_DELAY_STATUS,
    ("backpack", "dvr-aux-channel"): DVR_AUX_STATUS}
# Keys that carry a credential or a device identifier at this pin, plus a conservative
# shape test so a key this pin does not define is still never echoed.
SECRET_KEYS = frozenset({"uid", "wifi-ssid", "wifi-password", "ssid", "flash-discriminator"})
SECRET_SHAPE = re.compile(r"pass|secret|key|token|psk|phrase|credential|ssid|uid|discriminator", re.I)


def text_hex(value):
    return "hex:" + value.encode("utf-8").hex()


def secret_key(name):
    return name in SECRET_KEYS or bool(SECRET_SHAPE.search(name))


def bounded(value, name, bits):
    if type(value) is bool:
        value = int(value)
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError(f"{name} outside the {bits}-bit configured range")
    return value


def rate_row(value, radio):
    """A rate index has no meaning without the radio the export does not carry."""
    if radio is None:
        return "", RATE_UNRESOLVED, "", RATE_UNRESOLVED
    labels = RATE_TABLES[radio]
    if value >= len(labels):
        return "", "UNKNOWN_ENUM_RETAINED", "", RATE_BASIS
    label = labels[value]
    return label, "REPORTED", re.search(r"_([0-9]+)HZ", label).group(1), RATE_BASIS


def telemetry_row(value):
    if value >= len(TLM_LABELS):
        return "", "UNKNOWN_ENUM_RETAINED", ""
    # STD, NO_TLM and DISARMED have no numeric denominator; absence is never zero.
    return TLM_LABELS[value], "REPORTED", TLM_DENOMINATORS.get(value, "")


def convert_elrs_settings(text, *, source_format, capture_time_us, role, radio=None):
    """Selected settings by name; the document is never copied out wholesale."""
    # Declared, never inferred: the shape of a document does not identify a release.
    if source_format != SOURCE_FORMAT:
        raise ValueError(f"declared source format must be exactly {SOURCE_FORMAT}")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit nonnegative signed64 capture microseconds required")
    if role not in ("tx", "rx"):
        raise ValueError("declared tx or rx role required")
    if radio is not None and radio not in RATE_TABLES:
        raise ValueError("unsupported declared radio")
    raw = text.encode("utf-8")
    if len(raw) > LIMIT:
        raise ValueError("saved ExpressLRS export exceeds 16MiB")
    # Refuse a document that repeats a key: json.loads would keep only the last value.
    document = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(document, dict) or not isinstance(document.get("config"), dict):
        raise ValueError("selected ExpressLRS /config document required")
    config = document["config"]
    models = config.get("model")
    if role == "rx":
        # Every selected field below is emitted only under TARGET_TX at this pin, so any
        # of them contradicts the declared role rather than describing a receiver.
        present = [name for name in ("model", *(item[0] for item in DEVICE_FIELDS),
                                     *(group for group, _ in NESTED_DEVICE_FIELDS))
                   if name in config]
        if present:
            raise ValueError("declared rx role contradicts transmitter export fields")
        raise ValueError("the pinned receiver export carries no selected settings")
    if models is not None and not isinstance(models, dict):
        raise ValueError("transmitter model block must be a string-keyed object")
    source = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(COLUMNS)
    rows, handled = 0, set()
    accounting = {"retained": [], "redacted": [], "unknown_retained": []}

    def emit(scope, model, path, value, label="", status="REPORTED", hertz="", basis="",
             denominator=""):
        nonlocal rows
        writer.writerow([capture_time_us, rows, scope, "" if model is None else model,
                         text_hex(path), value, label, status, hertz, basis, denominator, source])
        rows += 1
        accounting["retained"].append(path)
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted ExpressLRS CSV exceeds 128MiB")

    def redact(path, value):
        # Accounting only: a fixed reason, the path and how many members were skipped.
        # No digest of the value, because a short credential is recoverable from its
        # hash, and no member key names, which settings meaning does not need.
        entry = {"path": path, "reason": REDACTION_REASON}
        if isinstance(value, (dict, list)):
            entry["member_count"] = len(value)
        accounting["redacted"].append(entry)

    def account(prefix, container):
        """Every key is classified once: emitted, redacted, or unknown and retained."""
        for name, value in container.items():
            path = f"{prefix}{name}"
            # A block holding any credential key is redacted whole, not walked into.
            if secret_key(name) or (isinstance(value, dict) and any(secret_key(key) for key in value)):
                redact(path, value)
            elif path in accounting["retained"] or path in handled:
                continue
            else:
                # Never inline an unknown value, whatever its JSON type. A number can be a
                # PIN, a one-time code or a numeric binding identifier, so type says
                # nothing about sensitivity. It is linked back to the retained original by
                # path and whole-document digest rather than copied out.
                accounting["unknown_retained"].append(
                    {"path": path, "status": REFERENCE_ONLY, "source_sha256": source})

    for name, bits in DEVICE_FIELDS:
        if name in config:
            emit("DEVICE", None, f"config.{name}", bounded(config[name], name, bits))
    for group, names in NESTED_DEVICE_FIELDS:
        block = config.get(group)
        if block is not None:
            if not isinstance(block, dict):
                raise ValueError(f"{group} must be an object")
            handled.add(f"config.{group}")
            for name in names:
                if name in block:
                    emit("DEVICE", None, f"config.{group}.{name}", bounded(block[name], name, 8),
                         status=NESTED_FIELD_STATUS[(group, name)])
            account(f"config.{group}.", block)
    for key in sorted(models or {}, key=lambda item: (len(item), item)):
        if not re.fullmatch(r"(0|[1-9][0-9]?)", key) or not 0 <= int(key) < MODEL_COUNT:
            raise ValueError("model key outside the configured model range")
        model, entry = int(key), models[key]
        if not isinstance(entry, dict):
            raise ValueError("model entry must be an object")
        for name, bits in MODEL_FIELDS:
            if name not in entry:
                continue
            value = bounded(entry[name], name, bits)
            path = f"config.model.{key}.{name}"
            if name == "packet-rate":
                label, status, hertz, basis = rate_row(value, radio)
                emit("MODEL", model, path, value, label, status, hertz, basis)
            elif name == "telemetry-ratio":
                label, status, denominator = telemetry_row(value)
                emit("MODEL", model, path, value, label, status, denominator=denominator)
            elif name == "switch-mode":
                label = SWITCH_LABELS[value] if value < len(SWITCH_LABELS) else ""
                emit("MODEL", model, path, value, label,
                     "REPORTED" if label else "UNKNOWN_ENUM_RETAINED")
            else:
                emit("MODEL", model, path, value)
        power = entry.get("power")
        if power is not None:
            if not isinstance(power, dict):
                raise ValueError("model power block must be an object")
            handled.add(f"config.model.{key}.power")
            for name, bits in MODEL_POWER_FIELDS:
                if name in power:
                    emit("MODEL", model, f"config.model.{key}.power.{name}",
                         bounded(power[name], name, bits))
            account(f"config.model.{key}.power.", power)
        account(f"config.model.{key}.", entry)
    handled.add("config.model")
    account("config.", config)
    account("", {name: value for name, value in document.items() if name != "config"})
    if not rows:
        raise ValueError("no selected ExpressLRS settings")
    report = {
        "source": PIN, "producer": "devWIFI.cpp GetConfiguration /config",
        "declared_source_format": source_format, "source_format_basis": SOURCE_FORMAT_BASIS,
        "scope": SCOPE, "duplicate_key_policy": "a repeated JSON key is refused, not last-wins",
        "source_sha256": source, "input_bytes": len(raw), "output_records": rows,
        "declared_role": role, "declared_radio": radio or "NOT_DECLARED",
        "packet_rate_basis": RATE_BASIS if radio else RATE_UNRESOLVED,
        "models_present": len(models or {}), "model_capacity": MODEL_COUNT,
        "redacted_fields": accounting["redacted"],
        "unknown_retained_fields": accounting["unknown_retained"],
        "retained_setting_count": len(accounting["retained"]),
        "secret_policy": "a credential or identifier key yields only a fixed reason, its path and a member count: no value, no value digest and no member key names; an unknown value is never inlined whatever its JSON type, because a number can be a PIN, a one-time code or a numeric binding identifier, and is reference-only against the retained original, which stays in operator-controlled private storage; only source-qualified whitelisted settings reach a common row after their own bitfield and enum validation",
        "clock": "Unknown", "time_basis": "CALLER_SUPPLIED_CAPTURE_NOT_SOURCE_QUALIFIED",
        "evidence_kind": "reported-configuration-not-device-change-or-link-health"}
    if len(json.dumps(report).encode()) > LIMIT:
        raise ValueError("ExpressLRS settings report exceeds bound")
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved /config export; no device is contacted")
    parser.add_argument("output", type=Path, help="new directory for observations.csv and report.json")
    parser.add_argument("--source-format", required=True,
                        help=f"declared source format; {SOURCE_FORMAT} is the only supported one")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="explicit caller capture clock; the export carries no time")
    parser.add_argument("--role", required=True, choices=("tx", "rx"), help="declared export role")
    parser.add_argument("--radio", choices=sorted(RATE_TABLES),
                        help="declared radio; without it a packet rate index stays unresolved")
    args = parser.parse_args(argv)
    with args.input.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("saved ExpressLRS export exceeds 16MiB")
    text, report = convert_elrs_settings(raw.decode("utf-8"), source_format=args.source_format,
        capture_time_us=args.capture_time_us, role=args.role, radio=args.radio)
    args.output.mkdir()
    (args.output / "observations.csv").write_text(text, encoding="utf-8", newline="")
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                             encoding="utf-8", newline="")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
