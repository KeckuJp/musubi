"""Make explicit nonfinite literals in recorded JSONL safe text, never numbers."""
import argparse
import json
import math
from pathlib import Path

try:
    from scripts.normalize_ardupilot_report import unique_object
except ModuleNotFoundError:
    from normalize_ardupilot_report import unique_object

INPUT_LIMIT = 64 * 1024 * 1024
OUTPUT_LIMIT = 512 * 1024 * 1024


class Nonfinite(str):
    pass


def convert(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded JSONL export required")
    output, size = [], 0
    for line in raw.splitlines():
        if not line or len(line) > 1024 * 1024:
            raise ValueError("blank or oversized export line")
        record = json.loads(line.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=Nonfinite)
        if not isinstance(record, dict) or not isinstance(record.get("meta"), dict) or not isinstance(record.get("data"), dict):
            raise ValueError("decoded meta/data record required")
        literals = {}

        def walk(value, path):
            if isinstance(value, Nonfinite):
                literals[path] = str(value)
                return str(value)
            if isinstance(value, dict):
                return {key: walk(item, path + "/" + key.replace("~", "~0").replace("/", "~1"))
                        for key, item in value.items()}
            if isinstance(value, list):
                return [walk(item, path + "/" + str(index)) for index, item in enumerate(value)]
            if type(value) is float and not math.isfinite(value):
                raise ValueError("numeric overflow is not an explicit nonfinite literal")
            return value

        normalized = walk(record, "")
        if literals:
            if "nonfinite_json_literals" in normalized["meta"] or "source_nonfinite_export_line_hex" in normalized:
                raise ValueError("reserved source retention collision")
            normalized["meta"]["nonfinite_json_literals"] = literals
            normalized["source_nonfinite_export_line_hex"] = "hex:" + line.hex()
            encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False).encode()
        else:
            encoded = line
        size += len(encoded) + 1
        if size > OUTPUT_LIMIT:
            raise ValueError("normalized export exceeds bound")
        output.append(encoded)
    return b"\n".join(output) + b"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as source:
            raw = source.read(INPUT_LIMIT + 1)
        output = convert(raw)
        with args.output.open("xb") as destination:
            destination.write(output)
    except (ValueError, OSError, RecursionError):
        parser.exit(2, "nonfinite export normalization failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
