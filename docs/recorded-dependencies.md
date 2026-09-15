# Recorded-input dependencies

Rust crates use the versions and license evidence recorded in `Cargo.lock` and
`sbom/dependency-sbom.json`. That generated SBOM covers the Cargo lockfile; it does
not describe the optional Python environment.

The basic CSV/JSON/XML converters use the Python standard library. Some saved formats
need additional libraries: ROS bags, AIS, DBC, passive DroneCAN, decoded DataFlash,
protobuf, packet captures and shapefiles. Install only the requirements for the
selected converter in a virtual environment. The complete authored-test environment
is pinned in `scripts/requirements-recorded-tests.txt` for Python 3.12.

```sh
python3.12 -m venv .venv-recorded
. .venv-recorded/bin/activate
python -m pip install -r scripts/requirements-recorded-tests.txt
```

These packages are separately installed; their source, binaries and datasets are not
bundled in this source preview. Preserve their applicable license and notice files
when redistributing an environment. In particular, pymavlink and python-can declare
LGPL licenses; the Apache-2.0 license on authored Musubi files does not replace them.

The table records installed distribution declarations, not a legal determination.

| Distribution | Version | Declared license |
|---|---|---|
| apsw | 3.53.4.0 | any-OSI (distribution declaration) |
| argparse-addons | 0.12.0 | MIT |
| attrs | 25.3.0 | MIT |
| bitstruct | 8.23.0 | MIT |
| cantools | 40.7.1 | MIT |
| crccheck | 1.3.1 | MIT |
| diskcache | 5.6.3 | Apache 2.0 |
| dpkt | 1.9.8 | BSD |
| dronecan | 1.0.27 | MIT |
| fastcrc | 0.3.2 | MIT License |
| lxml | 6.1.3 | BSD-3-Clause |
| lz4 | 4.4.5 | BSD |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| protobuf | 6.33.5 | BSD-3-Clause |
| pyais | 3.2.2 | MIT |
| pymavlink | 2.4.49 | LGPLv3 |
| pyshp | 2.3.1 | MIT |
| python-can | 4.6.1 | LGPL-3.0-only |
| rosbags | 0.11.5 | Apache-2.0 |
| ruamel.yaml | 0.19.1 | MIT |
| textparser | 0.26.2 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| wrapt | 1.17.3 | BSD |
| zstandard | 0.25.0 | BSD-3-Clause |

The hardware decoders and vendor applications that produced saved inputs are also
separate prerequisites. Decoder completion does not authenticate the source or verify
every firmware version. Synthetic tests do not establish live device compatibility.
