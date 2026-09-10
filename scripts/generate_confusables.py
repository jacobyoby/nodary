#!/usr/bin/env python3
"""Compile a runtime confusables map from the pinned UTS #39 data drop.

Reads data/uts39/confusables.txt (never the network) and writes
src/nodary/confusables_generated.py.

Usage:
    uv run python scripts/generate_confusables.py
    uv run python scripts/generate_confusables.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_VERSION = "17.0.0"
SOURCE_REL = Path("data/uts39/confusables.txt")
VERSION_REL = Path("data/uts39/VERSION")
OUTPUT_REL = Path("src/nodary/confusables_generated.py")
PINNED_SHA256 = "091c7f82fc39ef208faf8f94d29c244de99254675e09de163160c810d13ef22a"

_VERSION_RE = re.compile(r"^#\s*Version:\s*(\S+)", re.MULTILINE)
_MAPPING_RE = re.compile(
    r"^([0-9A-Fa-f]+(?:\s+[0-9A-Fa-f]+)*)\s*;"
    r"\s*([0-9A-Fa-f]+(?:\s+[0-9A-Fa-f]+)*)\s*;"
)


def _hex_seq_to_str(field: str) -> str:
    return "".join(chr(int(part, 16)) for part in field.split())


def parse_confusables(text: str) -> tuple[str, dict[str, str]]:
    match = _VERSION_RE.search(text)
    version = match.group(1) if match else "unknown"
    mappings: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parsed = _MAPPING_RE.match(line)
        if parsed is None:
            continue
        src = _hex_seq_to_str(parsed.group(1))
        tgt = _hex_seq_to_str(parsed.group(2))
        if src:
            mappings[src] = tgt
    return version, mappings


def runtime_key(src: str) -> str | None:
    """Key used after skeleton() lowercases input, or None to skip.

    UTS #39 is case-sensitive. Uppercase sources (I → l) must not be
    attached to the lowercase letter, or every Latin 'i' becomes 'l'.
    Filtering uses only case and combining class so 3.12 and 3.13 emit
    the same snapshot (NFKD tables differ across Unicode versions).
    """
    if len(src) != 1:
        return None
    if src != src.lower():
        return None
    if unicodedata.combining(src):
        return None
    return src


def build_confusables(uts_mappings: dict[str, str]) -> dict[str, str]:
    table: dict[str, str] = {}
    for src, tgt in uts_mappings.items():
        src_key = runtime_key(src)
        if src_key is None:
            continue
        tgt_val = tgt.lower()
        if not tgt_val or src_key == tgt_val:
            continue
        table[src_key] = tgt_val
    return dict(sorted(table.items(), key=lambda item: ord(item[0])))


def _py_str(value: str) -> str:
    parts: list[str] = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            parts.append('\\"')
        elif ch == "\\":
            parts.append("\\\\")
        elif ch.isascii() and 32 <= code < 127:
            parts.append(ch)
        elif code <= 0xFFFF:
            parts.append(f"\\u{code:04x}")
        else:
            parts.append(f"\\U{code:08x}")
    parts.append('"')
    return "".join(parts)


def render_module(
    *,
    version: str,
    sha256: str,
    source_rel: Path,
    confusables: dict[str, str],
) -> str:
    lines = [
        '"""Generated confusable map. Do not edit by hand.',
        "",
        "Regenerate with: uv run python scripts/generate_confusables.py",
        f"Source: {source_rel.as_posix()}",
        f"Unicode Security Mechanisms: {version}",
        f"Source SHA-256: {sha256}",
        f"Total mappings: {len(confusables)}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'UNICODE_SECURITY_VERSION = "{version}"',
        f'SOURCE_PATH = "{source_rel.as_posix()}"',
        f'SOURCE_SHA256 = "{sha256}"',
        "",
        "CONFUSABLES_MAP: dict[str, str] = {",
    ]
    for src, tgt in confusables.items():
        lines.append(f"    {_py_str(src)}: {_py_str(tgt)},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def verify_source(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != PINNED_SHA256:
        raise SystemExit(
            f"{path}: SHA-256 {digest} does not match pinned {PINNED_SHA256}"
        )
    return digest


def generate(repo_root: Path) -> str:
    source = repo_root / SOURCE_REL
    digest = verify_source(source)
    pinned = (repo_root / VERSION_REL).read_text(encoding="utf-8").strip()
    if pinned != PINNED_VERSION:
        raise SystemExit(f"{VERSION_REL}: {pinned!r} != pinned {PINNED_VERSION!r}")
    version, mappings = parse_confusables(source.read_text(encoding="utf-8"))
    if version != PINNED_VERSION:
        raise SystemExit(
            f"{source}: header version {version!r} != pinned {PINNED_VERSION!r}"
        )
    confusables = build_confusables(mappings)
    return render_module(
        version=version,
        sha256=digest,
        source_rel=SOURCE_REL,
        confusables=confusables,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the generated snapshot is stale (no write)",
    )
    args = parser.parse_args(argv)
    rendered = generate(REPO_ROOT)
    output = REPO_ROOT / OUTPUT_REL
    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != rendered:
            print(
                f"{OUTPUT_REL.as_posix()} is stale; run "
                "uv run python scripts/generate_confusables.py",
                file=sys.stderr,
            )
            return 1
        print(
            f"{OUTPUT_REL.as_posix()} matches {SOURCE_REL.as_posix()} "
            f"({PINNED_VERSION}, {PINNED_SHA256[:12]}…)"
        )
        return 0
    output.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {output} "
        f"({rendered.count(chr(10) + '    ')} mappings from UTS #39 {PINNED_VERSION})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
