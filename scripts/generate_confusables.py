#!/usr/bin/env python3
"""Compile a runtime confusables map from the pinned UTS #39 data drop.

Reads third_party/unicode/uts39/<version>/confusables.txt (never the network)
and writes src/nodary/feature_extraction/_confusables_data.py.

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
SOURCE_REL = Path("third_party/unicode/uts39") / PINNED_VERSION / "confusables.txt"
OUTPUT_REL = Path("src/nodary/feature_extraction/_confusables_data.py")
PINNED_SHA256 = "091c7f82fc39ef208faf8f94d29c244de99254675e09de163160c810d13ef22a"

_VERSION_RE = re.compile(r"^#\s*Version:\s*(\S+)", re.MULTILINE)
_MAPPING_RE = re.compile(
    r"^([0-9A-Fa-f]+(?:\s+[0-9A-Fa-f]+)*)\s*;"
    r"\s*([0-9A-Fa-f]+(?:\s+[0-9A-Fa-f]+)*)\s*;"
)

# Historical ASCII folds from the pre-UTS curated table. UTS #39 sometimes
# maps these to non-ASCII prototypes (small capitals, kra, …); for domain and
# display-name skeletons we keep the basic-Latin targets so lookalikes still
# collide with the ASCII domains users actually trust. Digit 3/5 substitutions
# are not in UTS #39 at all; 0→o and 1→l are.
ASCII_OVERLAYS: dict[str, str] = {
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "p",
    "с": "c",
    "х": "x",
    "у": "y",
    "і": "i",
    "ѕ": "s",
    "ј": "j",
    "ԁ": "d",
    "ɡ": "g",
    "һ": "h",
    "к": "k",
    "м": "m",
    "т": "t",
    "в": "b",
    "н": "h",
    "ѡ": "w",
    "ѵ": "v",
    "ꞅ": "s",
    "α": "a",
    "β": "b",
    "ε": "e",
    "η": "n",
    "ι": "i",
    "κ": "k",
    "ν": "v",
    "ο": "o",
    "ρ": "p",
    "τ": "t",
    "υ": "u",
    "ω": "w",
    "0": "o",
    "1": "l",
    "3": "e",
    "5": "s",
    "ø": "o",
    "ł": "l",
    "đ": "d",
    "ı": "i",
    "ǀ": "l",
    "ⅼ": "l",
    "ⅰ": "i",
    "ⅴ": "v",
}

_RESOLVE_LIMIT = 8


def fold_chars(text: str) -> str:
    """Same pre-map steps as runtime skeleton(): lower, NFKD, drop combining."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", text.lower()):
        if unicodedata.combining(ch):
            continue
        out.append(ch)
    return "".join(out)


def runtime_key(src: str) -> str | None:
    """Character looked up after skeleton()'s pre-map fold, or None to skip.

    Compatibility lookalikes (fullwidth, circled, math alphanumerics) already
    collapse under NFKD; using the leftover ASCII as a key would attach their
    UTS prototype to every ordinary Latin letter. Accented letters are skipped
    for the same reason: runtime strips the mark and looks up the base.
    """
    if len(src) != 1:
        return None
    # UTS #39 is case-sensitive. We lowercase before lookup, so an uppercase
    # source (I → l, etc.) must not be attached to the lowercase letter.
    if src != src.lower():
        return None
    ch = src
    if unicodedata.combining(ch):
        return None
    nfd_bases = [
        c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c)
    ]
    nfkd_bases = [
        c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
    ]
    if nfd_bases != [ch] or nfkd_bases != [ch]:
        return None
    return ch


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


def _resolve(text: str, table: dict[str, str]) -> str:
    current = text
    for _ in range(_RESOLVE_LIMIT):
        nxt = "".join(table.get(ch, ch) for ch in current)
        if nxt == current:
            return nxt
        current = nxt
    return current


def build_confusables(uts_mappings: dict[str, str]) -> dict[str, str]:
    """Fold UTS mappings + ASCII overlays into a runtime char→str table."""
    table: dict[str, str] = {}
    for src, tgt in uts_mappings.items():
        src_key = runtime_key(src)
        if src_key is None:
            continue
        tgt_val = fold_chars(tgt)
        if not tgt_val or src_key == tgt_val:
            continue
        table[src_key] = tgt_val

    for src, tgt in ASCII_OVERLAYS.items():
        src_key = runtime_key(src)
        if src_key is None:
            continue
        tgt_val = fold_chars(tgt)
        if not tgt_val:
            continue
        table[src_key] = tgt_val

    resolved: dict[str, str] = {}
    for src_key, tgt_val in table.items():
        final = _resolve(tgt_val, table)
        if final != src_key:
            resolved[src_key] = final
    return dict(sorted(resolved.items(), key=lambda item: (ord(item[0]), item[0])))


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
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'UNICODE_SECURITY_VERSION = "{version}"',
        f'SOURCE_PATH = "{source_rel.as_posix()}"',
        f'SOURCE_SHA256 = "{sha256}"',
        "",
        "CONFUSABLES: dict[str, str] = {",
    ]
    for src, tgt in confusables.items():
        lines.append(f"    {_py_str(src)}: {_py_str(tgt)},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def verify_source(path: Path) -> str:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != PINNED_SHA256:
        raise SystemExit(
            f"{path}: SHA-256 {digest} does not match pinned {PINNED_SHA256}"
        )
    return digest


def generate(repo_root: Path) -> str:
    source = repo_root / SOURCE_REL
    digest = verify_source(source)
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
    mappings = rendered.count("\n    ")
    print(f"wrote {output} ({mappings} mappings from UTS #39 {PINNED_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
