#!/usr/bin/env python3
"""Build the release artefacts into dist/:

* ``proxmox-node-exporter.pyz``            single-file exporter (zipapp)
* ``proxmox-node-exporter-<ver>.tar.gz``   exporter + installer + units +
                                           dashboards + alert rules
* ``SHA256SUMS``                           checksums of everything in dist/

Both archives are reproducible: rebuilding a tag yields identical bytes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from build_pyz import build as build_pyz  # noqa: E402
from proxmox_node_exporter import __version__  # noqa: E402

# Files shipped in the tarball, relative to the repository root.
INCLUDE = [
    "install.sh",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "packaging",
    "grafana",
    "prometheus",
    "deploy",
    "docs",
]


def _files() -> list[Path]:
    files: list[Path] = []
    for entry in INCLUDE:
        path = ROOT / entry
        if path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            files.append(path)
    return sorted(files)


def _tar_add(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def build_tarball(dist: Path, pyz: Path) -> Path:
    prefix = f"proxmox-node-exporter-{__version__}"
    output = dist / f"{prefix}.tar.gz"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        _tar_add(tar, f"{prefix}/{pyz.name}", pyz.read_bytes(), 0o755)
        for path in _files():
            rel = path.relative_to(ROOT).as_posix()
            mode = 0o755 if rel.endswith(".sh") else 0o644
            _tar_add(tar, f"{prefix}/{rel}", path.read_bytes(), mode)
    with (
        open(output, "wb") as fh,
        gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz,
    ):
        gz.write(raw.getvalue())
    return output


def write_checksums(dist: Path) -> Path:
    lines = []
    for path in sorted(dist.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}\n")
    output = dist / "SHA256SUMS"
    output.write_text("".join(lines))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--checksums-only",
        action="store_true",
        help="only (re)write SHA256SUMS, e.g. after adding wheels to dist/",
    )
    args = parser.parse_args()
    args.dist.mkdir(parents=True, exist_ok=True)
    if not args.checksums_only:
        pyz = build_pyz(args.dist / "proxmox-node-exporter.pyz")
        print(pyz)
        print(build_tarball(args.dist, pyz))
    print(write_checksums(args.dist))
    return 0


if __name__ == "__main__":
    sys.exit(main())
