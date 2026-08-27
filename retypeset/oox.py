"""
retypeset.oox -- direct OOXML inspection, used as ground truth for assets.

Why this module exists
----------------------
Pandoc is an excellent *text* reader for DOCX (it is the only mature OMML ->
LaTeX converter) but it is not a reliable *asset* reader. Measured on a real
manuscript, Pandoc 2.9 and 3.9 both silently discarded 4 of 13 embedded images
-- every EMF/WMF plus one PNG -- emitting no warning. A figure that vanishes
between the author's Word file and the submitted PDF is an unrecoverable
failure, so figure inventory must come from the OOXML itself.

Division of labour, therefore:
    Pandoc  -> prose, math, tables, lists, footnotes
    this    -> figures, their captions, their source order and dimensions

Both streams are reconciled in parse_docx.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
V = "urn:schemas-microsoft-com:vml"
# Word 2016+ stores an SVG as a vector extension hanging off a raster fallback:
#   <a:blip r:embed="rIdPNG"><a:extLst><a:ext><asvg:svgBlip r:embed="rIdSVG"/>
# Reading only a:blip therefore yields the fallback PNG and never the vector
# original -- and counting relationship references instead of pictures makes it
# look as though images went missing.
ASVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
NS = {"w": W, "r": R, "a": A, "wp": WP, "v": V, "asvg": ASVG}

# 914400 EMU per inch (OOXML fixed constant).
EMU_PER_IN = 914400.0
EMU_PER_MM = EMU_PER_IN / 25.4


@dataclass
class ImageRef:
    """One embedded image occurrence, in document order."""

    rid: str
    filename: str                 # basename inside word/media/ (raster fallback)
    para_index: int
    order: int
    width_emu: int = 0
    height_emu: int = 0
    anchored: bool = False        # floating (wp:anchor) vs. in-line (wp:inline)
    alt_text: str = ""
    in_table: bool = False
    # Vector original, when Word kept one alongside the raster fallback.
    svg_filename: str = ""

    @property
    def preferred(self) -> str:
        """The file a renderer should use: vector wins over raster."""
        return self.svg_filename or self.filename

    @property
    def width_mm(self) -> float:
        return self.width_emu / EMU_PER_MM if self.width_emu else 0.0

    @property
    def height_mm(self) -> float:
        return self.height_emu / EMU_PER_MM if self.height_emu else 0.0


@dataclass
class ParaInfo:
    index: int
    text: str
    style: str = ""
    images: list[ImageRef] = field(default_factory=list)
    in_table: bool = False
    bold_run_ratio: float = 0.0


@dataclass
class OoxScan:
    paragraphs: list[ParaInfo]
    images: list[ImageRef]
    media_files: dict[str, bytes]
    orphan_media: list[str]       # in word/media but never referenced in the body
    styles: dict[str, str]        # styleId -> human name


def _owns(p: etree._Element, node: etree._Element) -> bool:
    """True when `p` is the *nearest* w:p ancestor of `node`.

    Word nests paragraphs inside paragraphs (text boxes, shape captions,
    AlternateContent fallbacks). A naive `p.iter()` therefore attributes a child
    paragraph's runs and images to its parent as well, which double-counts
    images and concatenates neighbouring captions into one string. Both were
    observed on real input.
    """
    for anc in node.iterancestors():
        if not isinstance(anc.tag, str):
            continue
        if anc.tag == f"{{{W}}}p":
            return anc is p
    return False


def _text_of(p: etree._Element) -> str:
    """Visible text owned directly by this paragraph.

    Equations become "\ue000" (a private-use character) so that caption and
    heading heuristics can distinguish "paragraph is only an equation" from
    "paragraph is empty" without pulling in the math itself.
    """
    parts: list[str] = []
    for node in p.iter():
        if node is p or not isinstance(node.tag, str):
            continue
        tag = etree.QName(node).localname
        ns = node.tag.split("}")[0][1:] if "}" in node.tag else ""
        if tag not in ("t", "tab", "br", "cr", "oMath"):
            continue
        if not _owns(p, node):
            continue
        if tag == "t" and ns == W:
            parts.append(node.text or "")
        elif tag == "tab" and ns == W:
            parts.append("\t")
        elif tag in ("br", "cr") and ns == W:
            parts.append(" ")
        elif tag == "oMath":
            parts.append("\ue000")
    return "".join(parts)


def _bold_ratio(p: etree._Element) -> float:
    runs = p.findall(f".//{{{W}}}r")
    runs = [r for r in runs if (r.find(f".//{{{W}}}t") is not None)]
    if not runs:
        return 0.0
    bold = 0
    for r in runs:
        b = r.find(f"{{{W}}}rPr/{{{W}}}b")
        if b is not None and b.get(f"{{{W}}}val") not in ("0", "false"):
            bold += 1
    return bold / len(runs)


def _rels(z: zipfile.ZipFile) -> dict[str, str]:
    try:
        xml = z.read("word/_rels/document.xml.rels")
    except KeyError:
        return {}
    root = etree.fromstring(xml)
    out: dict[str, str] = {}
    for rel in root:
        tgt = rel.get("Target") or ""
        if "media/" in tgt:
            out[rel.get("Id")] = tgt.split("media/", 1)[1]
    return out


def _style_names(z: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = etree.fromstring(z.read("word/styles.xml"))
    except KeyError:
        return {}
    out = {}
    for s in root.findall(f"{{{W}}}style"):
        sid = s.get(f"{{{W}}}styleId") or ""
        nm = s.find(f"{{{W}}}name")
        out[sid] = (nm.get(f"{{{W}}}val") if nm is not None else sid) or sid
    return out


def scan(docx_path: str | Path) -> OoxScan:
    """Walk word/document.xml in document order, recording paragraphs and images."""
    path = Path(docx_path)
    with zipfile.ZipFile(path) as z:
        rels = _rels(z)
        styles = _style_names(z)
        media = {
            n.split("media/", 1)[1]: z.read(n)
            for n in z.namelist()
            if n.startswith("word/media/")
        }
        root = etree.fromstring(z.read("word/document.xml"))

    body = root.find(f"{{{W}}}body")
    paragraphs: list[ParaInfo] = []
    images: list[ImageRef] = []

    if body is None:
        return OoxScan([], [], media, sorted(media), styles)

    # iter() yields elements in document order, which is exactly the reading
    # order we need; table paragraphs appear in place.
    idx = 0
    for p in body.iter(f"{{{W}}}p"):
        in_table = any(
            etree.QName(anc).localname == "tbl"
            for anc in p.iterancestors()
            if isinstance(anc.tag, str)
        )
        pstyle = p.find(f"{{{W}}}pPr/{{{W}}}pStyle")
        sid = pstyle.get(f"{{{W}}}val") if pstyle is not None else ""
        info = ParaInfo(
            index=idx,
            text=_text_of(p),
            style=styles.get(sid, sid or ""),
            in_table=in_table,
            bold_run_ratio=_bold_ratio(p),
        )

        # DrawingML pictures (modern Word) and VML imagedata (legacy / OLE).
        for blip in p.iter(f"{{{A}}}blip"):
            rid = blip.get(f"{{{R}}}embed") or blip.get(f"{{{R}}}link") or ""
            if rid not in rels or not _owns(p, blip):
                continue
            anchor = None
            for anc in blip.iterancestors():
                ln = etree.QName(anc).localname if isinstance(anc.tag, str) else ""
                if ln in ("inline", "anchor"):
                    anchor = anc
                    break
            w_emu = h_emu = 0
            alt = ""
            if anchor is not None:
                ext = anchor.find(f"{{{WP}}}extent")
                if ext is not None:
                    w_emu = int(ext.get("cx") or 0)
                    h_emu = int(ext.get("cy") or 0)
                docpr = anchor.find(f"{{{WP}}}docPr")
                if docpr is not None:
                    alt = docpr.get("descr") or ""
            svg_name = ""
            for sb in blip.iter(f"{{{ASVG}}}svgBlip"):
                srid = sb.get(f"{{{R}}}embed") or sb.get(f"{{{R}}}link") or ""
                if srid in rels:
                    svg_name = rels[srid]
                    break

            images.append(ImageRef(
                rid=rid, filename=rels[rid], para_index=idx, order=len(images),
                width_emu=w_emu, height_emu=h_emu,
                anchored=(anchor is not None
                          and etree.QName(anchor).localname == "anchor"),
                alt_text=alt, in_table=in_table, svg_filename=svg_name,
            ))

        for vml in p.iter(f"{{{V}}}imagedata"):
            rid = vml.get(f"{{{R}}}id") or ""
            if rid in rels and _owns(p, vml):
                images.append(ImageRef(
                    rid=rid, filename=rels[rid], para_index=idx,
                    order=len(images), in_table=in_table,
                    alt_text=vml.get(f"{{{V}}}title") or "",
                ))

        info.images = [im for im in images if im.para_index == idx]
        paragraphs.append(info)
        idx += 1

    used = {im.filename for im in images} | {im.svg_filename for im in images if im.svg_filename}
    orphans = sorted(f for f in media if f not in used)
    return OoxScan(paragraphs, images, media, orphans, styles)


# Caption pattern, duplicated here so this module stays independent.
_CAP = re.compile(
    r"(?P<kind>fig(?:ure)?|tab(?:le)?|scheme|chart)\s*\.?\s*"
    r"(?P<num>[A-Z]?\d+(?:[.\-]\d+)?)\s*[.:\-–—)]?\s*(?P<rest>.*)",
    re.I | re.S,
)


def caption_for(scan_result: OoxScan, para_index: int, kind: str = "fig",
                window: int = 3) -> tuple[str, str, str]:
    """Find the caption belonging to a float at `para_index`.

    Returns (full_caption, label, body). Word authors put figure captions in the
    same paragraph as the image about as often as in the next one, so the search
    order is: same paragraph, then following paragraphs, then preceding ones.

    Empty string means no caption was found; the caller must raise an issue
    rather than inventing one.
    """
    want_table = kind.lower().startswith("tab")
    order = [para_index] + \
            [para_index + d for d in range(1, window + 1)] + \
            [para_index - d for d in range(1, window + 1)]

    for i in order:
        if not (0 <= i < len(scan_result.paragraphs)):
            continue
        text = scan_result.paragraphs[i].text.strip()
        if not text:
            continue
        m = _CAP.search(text)
        if not m:
            continue
        is_tab = m.group("kind").lower().startswith("tab")
        if is_tab != want_table:
            continue
        label = f"{m.group('kind')} {m.group('num')}"
        full = text[m.start():].strip()
        return full, label, m.group("rest").strip()
    return "", "", ""


def looks_like_path(s: str) -> bool:
    """True for Word alt-text that is really a leftover filesystem path.

    Word copies the source path of a pasted image into `descr`, and Pandoc then
    surfaces it as alt text. Treating those strings as captions produces
    manuscripts containing "E:\\Classe B\\...\\IMG-20260613-WA0007.jpg".
    """
    s = s.strip()
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", s)
        or s.startswith(("\\\\", "/Users/", "/home/", "file://"))
        or re.search(r"\.(png|jpe?g|gif|bmp|tiff?|emf|wmf|pdf|eps)\s*$", s, re.I)
    )
