#!/usr/bin/env python3
"""Explicit offline JSONL boot-counter remapping; never infer clock semantics."""
import argparse
import json
import math
from pathlib import Path
import sys

MAX_INPUT = 16 * 1024 * 1024
MAX_LINE = 64 * 1024
MAX_PROFILE = 4096
MAX_DEPTH = 32
U64_MAX = (1 << 64) - 1


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("nonfinite number")


def check_depth(value, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("nesting limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite number")
    if isinstance(value, dict):
        for child in value.values():
            check_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_depth(child, depth + 1)


def decode(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                       parse_constant=invalid_constant)
    check_depth(value)
    return value


def remap(raw, profile):
    """Return all records, preserving each selected original as source_record.

    The caller must independently establish that the declared field is a boot
    counter, not UTC or GNSS time. Parsed values, not original bytes, are retained.
    """
    if not isinstance(profile, dict) or set(profile) != {
        "message_type", "counter_field", "counter_unit"
    }:
        raise ValueError("invalid profile")
    if any(not isinstance(profile[key], str) or not profile[key]
           for key in profile) or profile["counter_unit"] not in ("ms", "us"):
        raise ValueError("invalid profile")
    if len(json.dumps(profile).encode()) > MAX_PROFILE:
        raise ValueError("profile limit")
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("input limit")
    multiplier = 1000 if profile["counter_unit"] == "ms" else 1
    output, size, selected = [], 0, 0
    for line in raw.splitlines():
        if not line or len(line) > MAX_LINE:
            raise ValueError("line limit")
        record = decode(line)
        if not isinstance(record, dict) or not isinstance(record.get("meta"), dict) \
                or not isinstance(record["meta"].get("type"), str) \
                or not record["meta"]["type"] \
                or not isinstance(record.get("data"), dict):
            raise ValueError("invalid record")
        if record["meta"]["type"] == profile["message_type"]:
            if "source_record" in record:
                raise ValueError("reserved field collision")
            value = record["data"].get(profile["counter_field"])
            if type(value) is not int or value < 0 or value > U64_MAX // multiplier:
                raise ValueError("invalid counter")
            original = record
            data = dict(original["data"])
            data.pop("TimeMS", None)
            data.pop("TimeUS", None)
            data["TimeUS"] = value * multiplier
            record = {**original, "data": data, "source_record": original}
            selected += 1
        check_depth(record)
        encoded = json.dumps(record, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode()
        if len(encoded) > MAX_LINE:
            raise ValueError("output line limit")
        size += len(encoded) + 1
        if size > MAX_INPUT:
            raise ValueError("output limit")
        output.append(encoded)
    if not selected:
        raise ValueError("no selected records")
    return b"\n".join(output) + b"\n"


def bounded_read(path, limit):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file limit")
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        result = remap(bounded_read(args.input, MAX_INPUT),
                       decode(bounded_read(args.profile, MAX_PROFILE)))
        with args.output.open("xb") as stream:
            stream.write(result)
    except (OSError, ValueError, TypeError, RecursionError):
        print("counter remapping failed; verify profile, input and new output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
