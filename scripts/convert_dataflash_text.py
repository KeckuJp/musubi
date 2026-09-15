"""Bounded offline DFReader_text export; refuse any decoder line omission."""
import argparse
from importlib.metadata import version
import json
import math
from pathlib import Path
import re
import struct
import tempfile

MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 512 * 1024 * 1024


def convert(source, *, preserve_nonfinite=False):
    if version("pymavlink") != "2.4.49":
        raise ValueError("pymavlink2.4.49 required")
    from pymavlink.DFReader import DFReader_text, DFFormat
    if not source or len(source) > MAX_INPUT or not source.endswith(b"\n"):
        raise ValueError("bounded newline-terminated text required")
    text = source.decode("utf-8")
    if any(ord(c) < 32 and c not in "\r\n" for c in text):
        raise ValueError("control character in text capture")
    lines = text.splitlines()
    delimiter = ", " if lines[0].startswith("FMT, ") else ","
    if not lines[0].startswith("FMT" + delimiter):
        raise ValueError("FMT must be first, no discarded prefix")
    formats, ids = {}, set()
    expected = []
    trailing_text = {}
    for line in lines:
        if not line or len(line.encode()) > 12000:
            raise ValueError("blank or oversized line")
        parts = line.split(delimiter)
        kind = parts[0]
        if kind == "FMT":
            parts = line.split(delimiter, 5)
            if len(parts) != 6:
                raise ValueError("incomplete FMT")
            _, number, length, name, code, labels = parts
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,3}", name):
                raise ValueError("invalid FMT name")
            if name in formats or int(number) in ids or not 0 <= int(number) <= 255:
                raise ValueError("duplicate FMT binding")
            columns = labels.split(",")
            if (len(columns) != len(code) or len(set(columns)) != len(columns)
                    or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c) for c in columns)
                    or "mavpackettype" in columns or "a" in code):
                raise ValueError("unsupported FMT columns or array")
            fmt = DFFormat(int(number), name, int(length), code, labels)
            if not 3 <= int(length) <= 255 or int(length) != struct.calcsize(fmt.msg_struct) + 3:
                raise ValueError("FMT length disagrees with declared fields")
            formats[name] = fmt
            ids.add(int(number))
        elif kind not in formats:
            raise ValueError("undefined message or ragged text row")
        else:
            fmt = formats[kind]
            if len(parts) > len(fmt.columns) + 1 and fmt.format[-1:] in ("n", "N", "Z") and not any(
                    code in "nNZMa" for code in fmt.format[:-1]):
                # Numeric prefix plus one final string has an unambiguous split.
                # DFReader_text otherwise returns only the first text fragment.
                value = line.split(delimiter, len(fmt.columns))[-1]
                if len(value.encode("utf-8")) > {"n": 4, "N": 16, "Z": 64}[fmt.format[-1]]:
                    raise ValueError("trailing text exceeds declared byte width")
                trailing_text[len(expected)] = (fmt.columns[-1], value)
            elif len(parts) != len(fmt.columns) + 1:
                raise ValueError("undefined message or ragged text row")
        expected.append(kind)
    output = []
    output_bytes = 0
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "snapshot.log"
        path.write_bytes(source)
        decoder = DFReader_text(str(path), zero_time_base=True)
        try:
            for index, kind in enumerate(expected):
                message = decoder.recv_msg()
                if message is None or message.get_type() != kind:
                    raise ValueError("decoder omitted or reordered a source line")
                data = message.to_dict()
                data.pop("mavpackettype")
                meta = dict(type=kind)
                if index in trailing_text:
                    field, value = trailing_text[index]
                    data[field] = value
                    meta["reassembled_trailing_text"] = field
                if preserve_nonfinite:
                    nonfinite = {}
                    for key, value in data.items():
                        if type(value) is float and not math.isfinite(value):
                            data[key] = "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
                            nonfinite[key] = formats[kind].format[formats[kind].columns.index(key)]
                    if nonfinite:
                        meta["nonfinite_text_fields"] = nonfinite
                encoded = json.dumps(dict(meta=meta, data=data,
                    source_line=index + 1, source_line_hex="hex:" + lines[index].encode().hex()),
                    ensure_ascii=False, allow_nan=False)
                output_bytes += len(encoded.encode()) + 1
                if output_bytes > MAX_OUTPUT:
                    raise ValueError("expanded export exceeds bound")
                output.append(encoded)
            if decoder.recv_msg() is not None:
                raise ValueError("decoder produced unexpected records")
        finally:
            decoder.close()
    result = "\n".join(output) + "\n"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preserve-nonfinite", action="store_true", help="retain nonfinite floating fields as explicit text, never measured numbers")
    args = parser.parse_args()
    with args.input.open("rb") as source:
        raw = source.read(MAX_INPUT + 1)
    result = convert(raw, preserve_nonfinite=args.preserve_nonfinite)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(result)


if __name__ == "__main__":
    main()
