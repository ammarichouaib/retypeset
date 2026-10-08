#!/usr/bin/env python3
"""Reproducible experiments for the IJDL manuscript.

E1  Cross-converter baseline: how much of each source manuscript four open-source
    converters retain, judged against counts taken from the source OOXML package.
E2  Fault injection: detection rate of the source-verified audit (element counts +
    paragraph-corpus diff) for faults injected into copies of each manuscript.

Only counts are written out; no manuscript text leaves this script.
usage: python run_experiments.py M1.docx M2.docx [--labels M1 M2] [--out results]
"""
from __future__ import annotations
import argparse, copy, json, random, re, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path
from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
W = "{%s}" % NS["w"]

# ---------------------------------------------------------------- ground truth
# Identical rules to retypeset.audit._docx_ground_truth (v0.8.4).
def ground_truth(xml: str) -> dict:
    return {
        "equations": len(re.findall(r"<m:oMath[\s>]", xml)),
        "tables": len(re.findall(r"<w:tbl>", xml)),
        "pictures": len(re.findall(r"<pic:pic[\s>]", xml)) + len(re.findall(r"<v:imagedata[\s>]", xml)),
    }

def read_doc(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8", "ignore")

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()

def para_texts(xml: str, min_len: int = 25, only_txbx: bool = False) -> list[str]:
    """Paragraph texts; text-box paragraphs de-duplicated (mc:Choice and mc:Fallback
    carry the same text box twice)."""
    root = etree.fromstring(xml.encode("utf-8"))
    out, seen = [], set()
    for p in root.iter(W + "p"):
        # skip paragraphs inside mc:Fallback (duplicate of mc:Choice)
        anc = p.getparent(); in_fb = False; in_tx = False
        while anc is not None:
            if anc.tag == "{%s}Fallback" % NS["mc"]: in_fb = True
            if anc.tag == W + "txbxContent": in_tx = True
            anc = anc.getparent()
        if in_fb or (only_txbx and not in_tx):
            continue
        # own text only (exclude nested paragraphs in text boxes)
        t = "".join(x.text or "" for x in p.iter(W + "t") if _owner(x) is p)
        t = norm(t)
        if len(t) >= min_len and (t not in seen or not only_txbx):
            seen.add(t); out.append(t)
    return out

def _owner(node):
    a = node.getparent()
    while a is not None and a.tag != W + "p":
        a = a.getparent()
    return a

# ---------------------------------------------------------------- converters
NOISE = re.compile(r"(\\[,;: ]|\\quad|\\qquad|[\^_]\s*\{\s*\}|[{}\s\\])+")  # = retypeset _MATH_NOISE_RE
def _maths(node, acc):
    if isinstance(node, dict):
        if node.get("t") == "Math": acc.append(node["c"][1])
        for v in node.values(): _maths(v, acc)
    elif isinstance(node, list):
        for v in node: _maths(v, acc)
EQNUM = re.compile(r"^\(?\s*([A-Z]?\.?\d+(?:\.\d+)?)\s*\)?$")  # = retypeset _EQNUM_RE
def numbering_layouts(xml):
    """Strict XML-level version of Procedure B: every row has exactly one cell holding
    mathematics and no text; other cells empty or a bracketed number; >=1 number."""
    root = etree.fromstring(xml.encode("utf-8")); M = "{%s}" % NS["m"]
    n_t = n_e = 0; in_tables = 0
    for t in root.iter(W + "tbl"):
        in_tables += sum(1 for _ in t.iter(M + "oMath"))
        rows = t.findall(W + "tr"); ok = bool(rows) and len(rows) <= 40; numbered = e = 0
        for tr in rows:
            nm = nn = 0
            for c in tr.findall(W + "tc"):
                u = sum(1 for _ in c.iter(M + "oMath"))
                txt = "".join(x.text or "" for x in c.iter(W + "t")).strip()
                if u and not txt: nm += 1; e += u
                elif not u and EQNUM.match(txt): nn += 1
                elif not u and not txt: pass
                else: ok = False
            if nm != 1 or nn > 1: ok = False
            if not ok: break
            numbered += nn
        if ok and numbered: n_t += 1; n_e += e
    return {"layout_tables": n_t, "equations_in_layouts": n_e, "equations_in_any_table": in_tables}
def caption_paras(src):
    with zipfile.ZipFile(src) as z:
        sty = z.read("word/styles.xml").decode("utf-8", "ignore"); xml = z.read("word/document.xml")
    ids = set(re.findall(r'w:styleId="([^"]+)"[^>]*>\s*<w:name w:val="[Cc]aption"', sty))
    root = etree.fromstring(xml); out = []
    for p in root.iter(W + "p"):
        s = p.find(W + "pPr/" + W + "pStyle")
        if s is not None and s.get(W + "val") in ids:
            t = norm("".join(x.text or "" for x in p.iter(W + "t") if _owner(x) is p))
            if t: out.append(t)
    return out
PANDOC3 = None
def pandoc_bins():
    bins = {}
    if shutil.which("pandoc"):
        v = subprocess.run(["pandoc", "--version"], capture_output=True, text=True).stdout.split("\n")[0]
        bins[v] = "pandoc"
    try:
        import pypandoc
        p = pypandoc.get_pandoc_path()
        v = subprocess.run([p, "--version"], capture_output=True, text=True).stdout.split("\n")[0]
        bins[v] = p
    except Exception:
        pass
    return bins

def L(s): return re.sub(r"[^a-z0-9]", "", s.lower())
def text_retained(src_paras, out_text):
    o = L(out_text)
    return sum(1 for t in src_paras if L(t)[:40] in o)

def run_pandoc(binp, src, work):
    work.mkdir(parents=True, exist_ok=True)
    h = subprocess.run([binp, str(src), "-t", "html", "--mathml", f"--extract-media={work}/media", "-o", str(work/"out.html")],
                       capture_output=True, text=True)
    html = (work/"out.html").read_text(encoding="utf-8", errors="ignore")
    l = subprocess.run([binp, str(src), "-t", "latex", "-o", str(work/"out.tex")], capture_output=True, text=True)
    tex = (work/"out.tex").read_text(encoding="utf-8", errors="ignore")
    import html as _h, json as _j
    txt = re.sub(r"<[^>]+>", "", _h.unescape(re.sub(r"<math.*?</math>", "", html, flags=re.S)))
    ast = _j.loads(subprocess.run([binp, str(src), "-t", "json"], capture_output=True, text=True).stdout)
    segs = []; _maths(ast, segs)
    degenerate = sum(1 for s in segs if not NOISE.sub("", s).strip())
    plain = subprocess.run([binp, str(src), "-t", "plain", "--wrap=none"], capture_output=True, text=True).stdout
    pl = txt
    return {
        "equations": len(re.findall(r"<math[\s>]", html)),
        "tables": len(re.findall(r"<table[\s>]", html)),
        "pictures": len(re.findall(r"<img[\s>]", html)),
        "degenerate_math": degenerate,
        "warnings": len([x for x in (h.stderr + l.stderr).splitlines() if x.strip()]),
        "_text": pl, "_plain": plain,
    }

def run_libreoffice(src, work):
    work.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["soffice", "--headless", "--convert-to", "odt", "--outdir", str(work), str(src)],
                       capture_output=True, text=True, timeout=600)
    odt = next(work.glob("*.odt"))
    with zipfile.ZipFile(odt) as z:
        c = z.read("content.xml").decode("utf-8", "ignore")
    frames = re.findall(r"<draw:frame\b.*?</draw:frame>", c, flags=re.S)
    objs = sum(1 for f in frames if "<draw:object" in f)
    imgs = sum(1 for f in frames if "<draw:image" in f and "<draw:object" not in f)
    txt = re.sub(r"<[^>]+>", " ", c)
    return {"equations": objs, "tables": len(re.findall(r"<table:table[\s>]", c)), "pictures": imgs,
            "degenerate_math": None, "warnings": len([x for x in r.stderr.splitlines() if x.strip()]), "_text": txt}

def run_mammoth(src, work):
    import mammoth
    with open(src, "rb") as f:
        res = mammoth.convert_to_html(f)
    html = res.value
    with open(src, "rb") as f:
        raw = mammoth.extract_raw_text(f).value
    return {"equations": len(re.findall(r"<math[\s>]", html)), "tables": len(re.findall(r"<table[\s>]", html)),
            "pictures": len(re.findall(r"<img[\s>]", html)), "degenerate_math": None,
            "warnings": len(res.messages), "_text": raw}

# ---------------------------------------------------------------- E2 faults
def load_tree(xml): return etree.fromstring(xml.encode("utf-8"))
def dump(root): return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True).decode("utf-8")

def ancestor(node, tag):
    a = node.getparent()
    while a is not None and a.tag != tag: a = a.getparent()
    return a

def body_pictures(root):
    return [p for p in root.iter("{%s}pic" % NS["pic"])]

def fault(root, kind, rng):
    """Mutate root in place; return True if the fault could be applied."""
    if kind == "delete_picture":
        pics = body_pictures(root)
        if not pics: return False
        run = ancestor(rng.choice(pics), W + "r")
        run.getparent().remove(run); return True
    if kind == "delete_equation":
        eqs = list(root.iter("{%s}oMath" % NS["m"]))
        if not eqs: return False
        e = rng.choice(eqs); e.getparent().remove(e); return True
    if kind == "delete_table":
        t = list(root.iter(W + "tbl"))
        if not t: return False
        x = rng.choice(t); x.getparent().remove(x); return True
    if kind == "empty_textbox":
        tb = [t for t in root.iter(W + "txbxContent") if ancestor(t, "{%s}Fallback" % NS["mc"]) is None]
        tb = [t for t in tb if norm("".join(x.text or "" for x in t.iter(W + "t")))]
        if not tb: return False
        x = rng.choice(tb)
        # empty both representations of the same box (Choice and Fallback)
        txt = norm("".join(y.text or "" for y in x.iter(W + "t")))
        for t in list(root.iter(W + "txbxContent")):
            if norm("".join(y.text or "" for y in t.iter(W + "t"))) == txt:
                for p in list(t): t.remove(p)
                t.append(etree.SubElement(t, W + "p"))
        return True
    if kind == "delete_paragraph":
        ps = [p for p in root.iter(W + "p") if ancestor(p, W + "txbxContent") is None and ancestor(p, W + "tbl") is None
              and len(norm("".join(x.text or "" for x in p.iter(W + "t")))) >= 25
              and not list(p.iter("{%s}oMath" % NS["m"])) and not list(p.iter(W + "drawing"))]
        if not ps: return False
        p = rng.choice(ps); p.getparent().remove(p); return True
    if kind == "swap_equations":
        eqs = list(root.iter("{%s}oMath" % NS["m"]))
        eqs = [e for e in eqs if etree.tostring(e) != b""]
        if len(eqs) < 2: return False
        a, b = rng.sample(eqs, 2)
        if etree.tostring(a) == etree.tostring(b): return False
        ca, cb = copy.deepcopy(a), copy.deepcopy(b)
        a.getparent().replace(a, cb); b.getparent().replace(b, ca); return True
    if kind == "corrupt_equation":
        # count-preserving: replace the content of one equation with a single symbol
        eqs = [e for e in root.iter("{%s}oMath" % NS["m"]) if len(e)]
        if not eqs: return False
        e = rng.choice(eqs); M = "{%s}" % NS["m"]
        for c in list(e): e.remove(c)
        r = etree.SubElement(e, M + "r"); t = etree.SubElement(r, M + "t"); t.text = "x"
        return True
    raise ValueError(kind)

def swap_image_bytes(src_zip: Path, dst_zip: Path, rng) -> bool:
    with zipfile.ZipFile(src_zip) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/") and n.lower().endswith((".png", ".jpg", ".jpeg"))]
        if len(media) < 2: return False
        a, b = rng.sample(media, 2)
        with zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED) as out:
            for n in z.namelist():
                data = z.read(b if n == a else a if n == b else n)
                out.writestr(n, data)
    return True

def write_variant(src: Path, dst: Path, new_xml: str):
    with zipfile.ZipFile(src) as z, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
        for n in z.namelist():
            out.writestr(n, new_xml.encode("utf-8") if n == "word/document.xml" else z.read(n))

def audit_compare(src_xml: str, out_xml: str) -> dict:
    gs, go = ground_truth(src_xml), ground_truth(out_xml)
    count_loss = any(go[k] < gs[k] for k in gs)
    sp, op = para_texts(src_xml), set(para_texts(out_xml))
    text_loss = any(t not in op for t in sp)
    return {"count_loss": count_loss, "text_loss": text_loss, "detected": count_loss or text_loss}

FAULTS = ["delete_picture", "delete_equation", "delete_table", "empty_textbox", "delete_paragraph",
          "swap_equations", "corrupt_equation", "swap_image_bytes"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("docx", nargs="+"); ap.add_argument("--labels", nargs="*"); ap.add_argument("--out", default="results")
    ap.add_argument("--trials", type=int, default=20); ap.add_argument("--seed", type=int, default=20261007)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    labels = a.labels or [f"D{i+1}" for i in range(len(a.docx))]
    tmp = Path(tempfile.mkdtemp(prefix="exp_"))
    E1, E2 = {}, {}
    bins = pandoc_bins()
    for lab, f in zip(labels, a.docx):
        src = Path(f); xml = read_doc(src)
        gt = ground_truth(xml)
        tb = para_texts(xml, min_len=1, only_txbx=True)
        allp = para_texts(xml)
        caps = caption_paras(src)
        row = {"source": {**gt, **numbering_layouts(xml), "svg_originals": len(re.findall(r"<asvg:svgBlip[\s>]", xml)),
                          "textbox_paragraphs": len(tb), "textbox_words": sum(len(t.split()) for t in tb),
                          "caption_paragraphs": len(caps), "paragraphs_25plus": len(allp)}}
        convs = {}
        for v, b in bins.items():
            convs[v] = lambda s, w, b=b: run_pandoc(b, s, w)
        convs["LibreOffice " + subprocess.run(["soffice", "--version"], capture_output=True, text=True).stdout.split()[1]] = run_libreoffice
        import mammoth  # noqa
        convs["mammoth 1.13.0"] = run_mammoth
        for name, fn in convs.items():
            r = fn(src, tmp / lab / re.sub(r"\W+", "_", name))
            t = r.pop("_text"); plain = r.pop("_plain", None)
            if plain is not None:
                r["captions_in_plain_output"] = text_retained(caps, plain)
            r["textbox_paragraphs_retained"] = text_retained(tb, t)
            r["paragraphs_retained"] = text_retained(allp, t)
            row[name] = r
        E1[lab] = row
        # E2
        rng = random.Random(a.seed)
        res = {}
        for k in FAULTS:
            det = app = 0
            for i in range(a.trials):
                dst = tmp / f"{lab}_{k}_{i}.docx"
                if k == "swap_image_bytes":
                    ok = swap_image_bytes(src, dst, rng)
                    oxml = read_doc(dst) if ok else None
                else:
                    root = load_tree(xml); ok = fault(root, k, rng)
                    if ok:
                        oxml = dump(root); write_variant(src, dst, oxml)
                if not ok: continue
                app += 1
                det += audit_compare(xml, oxml)["detected"]
            res[k] = {"applied": app, "detected": det}
        # control: an unmodified copy, re-serialised exactly as the fault variants are, must not be flagged
        res["control_identical_copy"] = {"applied": 1, "detected": int(audit_compare(xml, dump(load_tree(xml)))["detected"])}
        E2[lab] = res
    (out / "results.json").write_text(json.dumps({"E1": E1, "E2": E2, "trials": a.trials, "seed": a.seed}, indent=2))
    print(json.dumps({"E1": E1, "E2": E2}, indent=1))

if __name__ == "__main__":
    main()
