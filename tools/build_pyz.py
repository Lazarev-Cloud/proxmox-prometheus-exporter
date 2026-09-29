#!/usr/bin/env python3
"""Build the exporter as a single-file, byte-for-byte reproducible zipapp.

The archive contains only the package's own sources (the exporter has no
third-party dependencies), stored uncompressed with fixed timestamps and
permissions in sorted order, so anyone can rebuild a release from its git tag
and compare the SHA-256 with the published SHA256SUMS.

    python3 tools/build_pyz.py dist/proxmox-node-exporter.pyz
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "proxmox_node_exporter"
SHEBANG = b"#!/usr/bin/env python3\n"
MAIN = b"import sys\n\nfrom proxmox_node_exporter.cli import main\n\nsys.exit(main())\n"
TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _add(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=TIMESTAMP)
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3  # Unix, regardless of the build host
    archive.writestr(info, data)


def build(output: Path) -> Path:
    sources = sorted(path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(SHEBANG)
        with zipfile.ZipFile(fh, "w") as archive:
            _add(archive, "__main__.py", MAIN)
            for path in sources:
                _add(archive, path.relative_to(PACKAGE.parent).as_posix(), path.read_bytes())
    os.chmod(tmp, 0o755)  # noqa: S103 - the zipapp is meant to be executable
    os.replace(tmp, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "output", type=Path, nargs="?", default=ROOT / "dist" / "proxmox-node-exporter.pyz"
    )
    args = parser.parse_args()
    path = build(args.output)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
