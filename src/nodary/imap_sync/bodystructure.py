"""Walk IMAP BODYSTRUCTURE responses without fetching bodies.

BODYSTRUCTURE already carries every structural fact we keep (MIME types,
sizes, filenames — of which only the extension is retained), so attachments
are never downloaded. Parsing is deliberately lenient: trailing fields vary
by server and by part type, so the disposition is located by shape, not by
position.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartInfo:
    section: str  # IMAP section number, e.g. '1', '2.1'
    maintype: str
    subtype: str
    encoding: str  # content-transfer-encoding, lowercased
    size: int | None
    charset: str | None
    is_attachment: bool
    filename_ext: str  # extension only; the filename is discarded

    @property
    def mime_type(self) -> str:
        return f"{self.maintype}/{self.subtype}"


def _as_str(v) -> str:
    if isinstance(v, bytes):
        return v.decode("ascii", errors="replace")
    return str(v) if v is not None else ""


def _params_dict(params) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(params, (list, tuple)):
        it = list(params)
        for k, v in zip(it[::2], it[1::2], strict=False):
            out[_as_str(k).lower()] = _as_str(v)
    return out


def _find_disposition(elements) -> tuple[bool, str]:
    """Scan trailing BODYSTRUCTURE fields for (DISPOSITION (params)).
    Returns (is_attachment, filename)."""
    for el in elements:
        if (
            isinstance(el, (list, tuple))
            and len(el) >= 1
            and isinstance(el[0], (bytes, str))
            and _as_str(el[0]).lower() in ("attachment", "inline")
        ):
            disp = _as_str(el[0]).lower()
            params = _params_dict(el[1] if len(el) > 1 else None)
            fname = params.get("filename", "")
            return disp == "attachment", fname
    return False, ""


def _child_parts(bs) -> list | None:
    """Return child parts when `bs` is multipart, else None. Handles both
    shapes seen in the wild: ([p1, p2, ...], subtype, ...) and
    ((p1), (p2), ..., subtype, ...)."""
    if not isinstance(bs, (list, tuple)) or not bs:
        return None
    first = bs[0]
    if isinstance(first, (bytes, str)):
        return None  # single part: first field is the maintype
    if (
        isinstance(first, (list, tuple))
        and first
        and isinstance(first[0], (list, tuple))
    ):
        return list(first)  # shape A: explicit list of parts
    parts = []
    for el in bs:
        if (
            isinstance(el, (list, tuple))
            and el
            and isinstance(el[0], (bytes, str, list, tuple))
        ):
            parts.append(el)
        else:
            break
    return parts or None


def walk(bs, prefix: str = "") -> list[PartInfo]:
    """Flatten a BODYSTRUCTURE into PartInfo entries with IMAP sections."""
    children = _child_parts(bs)
    if children is not None:
        out: list[PartInfo] = []
        for i, child in enumerate(children, 1):
            section = f"{prefix}{i}"
            grandkids = _child_parts(child)
            if grandkids is not None:
                out.extend(walk(child, prefix=f"{section}."))
            else:
                out.append(_leaf(child, section))
        return out
    return [_leaf(bs, prefix + "1" if not prefix else prefix.rstrip("."))]


def _leaf(bs, section: str) -> PartInfo:
    maintype = _as_str(bs[0]).lower() if len(bs) > 0 else "application"
    subtype = _as_str(bs[1]).lower() if len(bs) > 1 else "octet-stream"
    params = _params_dict(bs[2] if len(bs) > 2 else None)
    encoding = _as_str(bs[5]).lower() if len(bs) > 5 else ""
    size = bs[6] if len(bs) > 6 and isinstance(bs[6], int) else None
    is_att, filename = _find_disposition(bs[7:])
    if not filename:
        filename = params.get("name", "")
    if not is_att:
        # No explicit disposition: treat named non-text parts and embedded
        # messages as attachments.
        is_att = (bool(filename) and maintype != "text") or maintype == "message"
    ext = ""
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[1].lower()[:16]
    elif maintype == "message":
        ext = "eml"
    return PartInfo(
        section=section,
        maintype=maintype,
        subtype=subtype,
        encoding=encoding,
        size=size,
        charset=params.get("charset"),
        is_attachment=is_att,
        filename_ext=ext,
    )
