#!/usr/bin/env python3
"""Create the editable Figure 2 slide and its matching vector PDF."""

import argparse
from pathlib import Path
from shutil import copyfile
from subprocess import run
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
PACKAGE_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml."
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

SIZE = (13 + 1 / 3, 7.5)
INK = "172435"
FRAME = "B0BCC9"
PANEL = "F8FAFC"
HEADER = "E8F0F8"
SEED = "00588F"
CLOSURE = "673887"
DELETION = "A7431B"
CONTEXT = "626E7B"
WHITE = "FFFFFF"
FONT = "Times New Roman"


def xml(parent, tag, **attrs):
    prefix, name = tag.split(":")
    qualified = f"{{{NS[prefix]}}}{name}"
    if parent is None:
        return ET.Element(qualified, attrs)
    return ET.SubElement(parent, qualified, attrs)


def emu(inches):
    return str(round(914400 * inches))


def document(node):
    return ET.tostring(node, encoding="UTF-8", xml_declaration=True)


def transform(props, x, y, w, h, **attrs):
    xfrm = xml(props, "a:xfrm", **attrs)
    xml(xfrm, "a:off", x=emu(x), y=emu(y))
    xml(xfrm, "a:ext", cx=emu(w), cy=emu(h))
    return xfrm


def fill(parent, color):
    if color is None:
        xml(parent, "a:noFill")
    else:
        xml(xml(parent, "a:solidFill"), "a:srgbClr", val=color)


def shape_tree(parent):
    tree = xml(parent, "p:spTree")
    group = xml(tree, "p:nvGrpSpPr")
    xml(group, "p:cNvPr", id="1", name="")
    xml(group, "p:cNvGrpSpPr")
    xml(group, "p:nvPr")
    xfrm = transform(xml(tree, "p:grpSpPr"), 0, 0, 0, 0)
    xml(xfrm, "a:chOff", x="0", y="0")
    xml(xfrm, "a:chExt", cx="0", cy="0")
    return tree


class Slide:
    def __init__(self):
        self.root = xml(None, "p:sld")
        content = xml(self.root, "p:cSld", name="Batch maintenance")
        bg = xml(xml(content, "p:bg"), "p:bgPr")
        fill(bg, WHITE)
        xml(bg, "a:effectLst")
        self.tree = shape_tree(content)
        xml(xml(self.root, "p:clrMapOvr"), "a:masterClrMapping")
        self.next_id = 2

    def graphic(self, name, x, y, w, h, kind="sp", text_box=False, **orientation):
        shape = xml(self.tree, f"p:{kind}")
        if kind == "sp":
            nv = xml(shape, "p:nvSpPr")
            xml(nv, "p:cNvPr", id=str(self.next_id), name=name)
            xml(nv, "p:cNvSpPr", **({"txBox": "1"} if text_box else {}))
        else:
            nv = xml(shape, "p:nvCxnSpPr")
            xml(nv, "p:cNvPr", id=str(self.next_id), name=name)
            xml(nv, "p:cNvCxnSpPr")
        xml(nv, "p:nvPr")
        self.next_id += 1
        props = xml(shape, "p:spPr")
        transform(props, x, y, w, h, **orientation)
        return shape, props

    def box(self, name, x, y, w, h, *, color=WHITE, border=FRAME, kind="roundRect"):
        _, props = self.graphic(name, x, y, w, h)
        geom = xml(props, "a:prstGeom", prst=kind)
        adjustments = xml(geom, "a:avLst")
        if kind == "roundRect":
            xml(adjustments, "a:gd", name="adj", fmla="val 10500")
        fill(props, color)
        fill(xml(props, "a:ln", w="10000"), border)

    def text(self, label, x, y, w, h, *, size=12, color=INK, bold=False, align="ctr"):
        shape, props = self.graphic(label, x, y, w, h, text_box=True)
        xml(xml(props, "a:prstGeom", prst="rect"), "a:avLst")
        fill(props, None)
        fill(xml(props, "a:ln"), None)
        body = xml(shape, "p:txBody")
        xml(body, "a:bodyPr", wrap="none", anchor="ctr", lIns="0", tIns="0",
            rIns="0", bIns="0")
        xml(body, "a:lstStyle")
        paragraph = xml(body, "a:p")
        xml(paragraph, "a:pPr", algn=align)
        main, sep, tail = label.partition("S_D")
        chunks = [(label, False)] if not sep else [
            (main + "S", False), ("D", True), (tail, False)
        ]
        for content, subscript in chunks:
            if not content:
                continue
            run = xml(paragraph, "a:r")
            style = xml(run, "a:rPr", lang="en-US", sz=str(round(size * 100)),
                        b="1" if bold else "0")
            if subscript:
                style.set("baseline", "-23000")
            fill(style, color)
            xml(style, "a:latin", typeface=FONT)
            xml(run, "a:t").text = content

    def line(self, name, start, end, *, color=INK, width=1.3, dashed=False, arrow=False,
             elbow=False):
        x1, y1 = start
        x2, y2 = end
        _, props = self.graphic(name, min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1),
                                kind="cxnSp", flipH=str(int(x2 < x1)),
                                flipV=str(int(y2 < y1)))
        preset = "bentConnector3" if elbow else "line"
        xml(xml(props, "a:prstGeom", prst=preset), "a:avLst")
        stroke = xml(props, "a:ln", w=str(round(12700 * width)), cap="rnd")
        fill(stroke, color)
        if dashed:
            xml(stroke, "a:prstDash", val="dash")
        if arrow:
            xml(stroke, "a:tailEnd", type="triangle", w="med", len="med")

    def triangle(self, name, points, color):
        x = min(v[0] for v in points)
        y = min(v[1] for v in points)
        w = max(v[0] for v in points) - x
        h = max(v[1] for v in points) - y
        _, props = self.graphic(name, x, y, w, h)
        geometry = xml(props, "a:custGeom")
        for list_name in ("avLst", "gdLst", "ahLst", "cxnLst"):
            xml(geometry, f"a:{list_name}")
        xml(geometry, "a:rect", l="l", t="t", r="r", b="b")
        path = xml(xml(geometry, "a:pathLst"), "a:path", w=emu(w), h=emu(h), stroke="0")
        for i, (vx, vy) in enumerate(points):
            command = xml(path, "a:moveTo" if i == 0 else "a:lnTo")
            xml(command, "a:pt", x=emu(vx - x), y=emu(vy - y))
        xml(path, "a:close")
        fill(props, color)


TEX_GLYPHS = str.maketrans({
    "{": r"\{", "}": r"\}", "−": r"\ensuremath{-}", "→": r"\ensuremath{\to}",
    "′": r"\ensuremath{'}", "∪": r"\ensuremath{\cup}", "τ": r"\ensuremath{\tau}",
    "≥": r"\ensuremath{\geq}", "_": r"\_",
})


class VectorSlide:
    """Render the same layout to a cropped PDF page using TikZ primitives."""

    def __init__(self):
        self.commands = []
        self.colors = set()

    def color(self, hex_value):
        self.colors.add(hex_value)
        return "hex" + hex_value

    def box(self, name, x, y, w, h, *, color=WHITE, border=FRAME, kind="roundRect"):
        stroke = self.color(border)
        background = self.color(color)
        options = f"draw={stroke},fill={background},line width=.65bp"
        if kind == "ellipse":
            self.commands.append(
                rf"\path[{options}] ({x + w / 2},{y + h / 2}) ellipse ({w / 2} and {h / 2});")
        else:
            radius = 20 if name.endswith(" panel") else 3
            self.commands.append(
                rf"\path[{options},rounded corners={radius}bp] ({x},{y}) rectangle ({x+w},{y+h});")

    def text(self, label, x, y, w, h, *, size=12, color=INK, bold=False, align="ctr"):
        anchor = {"l": "west", "ctr": "center", "r": "east"}[align]
        position = {"l": x, "ctr": x + w / 2, "r": x + w}[align]
        weight = r"\bfseries" if bold else ""
        caption = label.translate(TEX_GLYPHS).replace(r"S\_D", r"\ensuremath{S_D}")
        self.commands.append(
            rf"\node[inner sep=0pt,anchor={anchor},text={self.color(color)},"
            rf"font=\fontsize{{{size}bp}}{{{size}bp}}\selectfont{weight}]"
            rf" at ({position},{y+h/2}) {{{caption}}};")

    def line(self, _name, start, end, *, color=INK, width=1.3, dashed=False, arrow=False,
             elbow=False):
        attrs = [f"draw={self.color(color)}", f"line width={width}bp", "line cap=round"]
        if dashed:
            attrs.append("dashed")
        if arrow:
            attrs.append("-{Stealth[length=5bp,width=4bp]}")
        points = [start]
        if elbow:
            center = (start[0] + end[0]) / 2
            points.extend(((center, start[1]), (center, end[1])))
        points.append(end)
        path = " -- ".join(f"({x},{y})" for x, y in points)
        self.commands.append(rf"\draw[{','.join(attrs)}] {path};")

    def triangle(self, _name, points, color):
        path = " -- ".join(f"({x},{y})" for x, y in points)
        self.commands.append(rf"\fill[{self.color(color)}] {path} -- cycle;")

    def tex(self):
        lines = [r"\documentclass{standalone}", r"\usepackage[T1]{fontenc}",
                 r"\usepackage{tgtermes}", r"\usepackage{tikz}", r"\usetikzlibrary{arrows.meta}",
                 *(rf"\definecolor{{hex{color}}}{{HTML}}{{{color}}}" for color in sorted(self.colors)),
                 r"\begin{document}", r"\begin{tikzpicture}[x=1in,y=-1in]",
                 # Retain the earlier paper figure's 907 bp by 447 bp crop.
                 r"\path[use as bounding box] (26bp,-67bp) rectangle (933bp,-514bp);",
                 r"\begin{pgfinterruptboundingbox}", *self.commands, r"\end{pgfinterruptboundingbox}",
                 r"\end{tikzpicture}", r"\end{document}"]
        return "\n".join(lines) + "\n"


def graph(slide, x, *, after):
    scale = 1.19 if after else .89
    vertices = {name: (x + scale * dx, y) for name, dx, y in (
        ("u", .73, 4.01), ("v", 2.13, 4.01), ("w", 1.52, 3.10),
        ("x", .73, 3.10), ("y", 3.03, 3.58))}
    example = "Batch-end" if after else "Old"
    tri = "uwx" if after else "uvw"
    slide.triangle(f"{example} triangle {tri}", [vertices[v] for v in tri],
                   "E5F1F9" if after else "F8E9E2")
    for edge in ("uw", "vw", "vy"):
        color = CONTEXT if edge == "vy" else SEED
        slide.line(f"{example} edge {edge}", vertices[edge[0]], vertices[edge[1]],
                   color=color, width=1.35 if color == CONTEXT else 2.5)
    if after:
        slide.line("New edge wx", vertices["w"], vertices["x"], color=SEED, width=2.5)
        slide.line("Reached edge ux", vertices["u"], vertices["x"], color=CLOSURE, width=4)
        slide.line("Reached edge ux inset", vertices["u"], vertices["x"], color=PANEL, width=1)
        slide.text("+", x + 1.05 * scale, 2.83, .22, .22, color=SEED, bold=True)
    else:
        slide.line("Deleted edge uv", vertices["u"], vertices["v"],
                   color=DELETION, width=2.3, dashed=True)
        slide.line("Old edge ux", vertices["u"], vertices["x"], color=CONTEXT, width=1.35)
        slide.text("−", x + 1.32 * scale, 4.11, .22, .22, color=DELETION, bold=True)
    offsets = {"u": (-.26, .08), "v": (.04, .08), "w": (-.11, -.28),
               "x": (-.28, -.22), "y": (.13, -.12)}
    for vertex, (cx, cy) in vertices.items():
        slide.box(f"{example} vertex {vertex}", cx - .054, cy - .054, .108, .108,
                  border=INK, kind="ellipse")
        dx, dy = offsets[vertex]
        slide.text(vertex, cx + dx, cy + dy, .22, .22, size=14)


def draw_framework(s, *, title=True):
    if title:
        s.text("Fused-Pruned batch maintenance", .40, .25, 12.5, .55,
               size=27, bold=True, align="l")
    s.box("Input strip", .40, .96, 12.53, .60, color=HEADER)
    s.text("Ordered batch  b = (−uv, +wx)", .55, 1.06, 3.65, .37, size=17)
    s.line("Ordered batch to reduction", (4.22, 1.25), (4.60, 1.25), arrow=True)
    s.text("[S]  Reduce by last event", 4.68, 1.06, 3.96, .37, size=17)
    s.line("Reduction to net sets", (8.66, 1.25), (9.04, 1.25), arrow=True)
    s.text("Net sets  D = {uv}, I = {wx}", 9.08, 1.06, 3.69, .37, size=17)

    old, new, output = .43, 4.01, 9.08
    for x, width, heading in [(old, 3.33, "(a)  Old graph G"),
                              (new, 4.80, "(b)  Batch-end graph G′"),
                              (output, 3.85, "(c)  Exact batch-end labels")]:
        s.box(heading + " panel", x, 1.79, width, 4.83, color=PANEL)
        s.text(heading, x, 1.93, width, .34, size=18, bold=True)

    s.text("[P]  Collect old triangle mates", old + .16, 2.36, 3.01, .27,
           size=14, color=SEED, bold=True)
    s.line("Old graph to batch-end graph", (3.76, 2.50), (4.01, 2.50), arrow=True)
    s.text("[S]  Delete D  →  filter live mates  →  insert I", new + .10, 2.36, 4.60, .27,
           size=14, bold=True)
    graph(s, old, after=False)
    graph(s, new, after=True)

    s.text("Triangle uvw is destroyed", old + .16, 4.53, 3.01, .27,
           color=DELETION, size=15)
    s.box("Old triangle mates", old + .22, 4.98, 2.89, 1.10)
    s.text("Old mates collected for uv", old + .22, 5.12, 2.89, .29, size=14)
    s.text("{uw, vw}", old + .22, 5.49, 2.89, .34, color=SEED, size=16, bold=True)

    for dx, heading, edges in [(0.22, "Surviving old mates", "S_D = {uw, vw}"),
                               (2.60, "Net insertions", "I = {wx}")]:
        s.box(heading, new + dx, 4.41, 1.98, .61)
        s.text(heading, new + dx, 4.45, 1.98, .23, size=13)
        s.text(edges, new + dx, 4.72, 1.98, .24, size=14, color=SEED, bold=True)
    center = new + 2.40
    junction = (center, 5.26)
    s.line("Old-mate branch", (new + 1.21, 5.02), junction, color=SEED, width=1.1)
    s.line("Net-insertion branch", (new + 3.59, 5.02), junction, color=SEED, width=1.1)
    s.line("Join into seed set", junction, (center, 5.37), color=SEED, arrow=True)
    s.box("Seed union", new + .82, 5.37, 3.16, .36, color=HEADER)
    s.text("Seeds  S = S_D ∪ I", new + .82, 5.39, 3.16, .31, size=14,
           color=SEED, bold=True)
    s.line("Seeds to closure", (center, 5.73), (center, 5.85), arrow=True)
    s.box("Fused closure and signature scan", new + .45, 5.85, 3.90, .64, color="F3EDF7")
    s.text("[P]  Expand in G′ and count supports", new + .45, 5.89, 3.90, .24,
           size=14, color=CLOSURE, bold=True)
    s.text("XOR mate pairs while finding R = S ∪ {ux}", new + .45, 6.17, 3.90, .24,
           size=13, color=CLOSURE)

    s.text("For nonempty R", output + .20, 2.36, 3.45, .27, size=14)
    s.box("Serial low-support queue", output + .19, 2.70, 3.47, .60)
    s.line("Fused scan to low-support queue", (new + 4.35, 6.17), (output + .19, 3.00),
           color=CLOSURE, arrow=True, dashed=True, elbow=True)
    s.text("[S]  Queue for low support", output + .19, 2.76, 3.47, .24, size=15, bold=True)
    s.text("Recover the last triangle by XOR", output + .19, 3.02, 3.47, .22, size=13)
    s.line("Queue to bucket peel", (output + 1.925, 3.30), (output + 1.925, 3.45), arrow=True)
    s.box("Serial bucket peel for surviving edges", output + .19, 3.45, 3.47, .62)
    s.text("[S]  Bucket-peel survivors (τ ≥ 4)", output + .19, 3.50, 3.47, .26,
           size=14, bold=True)
    s.text("No survivors in this batch", output + .19, 3.80, 3.47, .20, size=12)
    s.text("Exact labels in R", output + .20, 4.19, 3.45, .28, size=14,
           color=SEED, bold=True)
    for dx, y, edge, values in [(0.19, 4.53, "ux", "2 → 3"),
                                (1.98, 4.53, "vw", "3 → 2"),
                                (0.19, 5.05, "uw", "3 → 3"),
                                (1.98, 5.05, "wx", "new → 3")]:
        left = output + dx
        s.box(f"Recomputed label for {edge}", left, y, 1.68, .44)
        s.text(edge, left + .10, y + .065, .44, .31, size=14, bold=True, align="l")
        s.text(values, left + .54, y + .065, 1.04, .31, size=14, align="r")
    s.box("Reused edge label outside the region", output + .19, 5.77, 3.47, .60,
          color="EFF2F5")
    s.text("Reuse outside R", output + .30, 5.91, 1.62, .31,
           size=14, color=CONTEXT, bold=True, align="l")
    s.text("vy   2 → 2", output + 2.00, 5.91, 1.50, .31, size=14, align="r")

    legend = [(.46, "Net deletion", DELETION, 2, True),
              (2.53, "Seed edge", SEED, 2.4, False),
              (4.39, "Reached by closure", CLOSURE, 4, False),
              (6.87, "Outside R in G′", CONTEXT, 1.35, False)]
    for x, label, hue, width, dash in legend:
        s.line(label + " sample", (x, 6.97), (x + .36, 6.97),
               color=hue, width=width, dashed=dash)
        if hue == CLOSURE:
            s.line("Closure sample inset", (x, 6.97), (x + .36, 6.97), color=WHITE, width=1)
        s.text(label, x + .47, 6.83, 1.90, .27, size=12, align="l")
    s.text("[P] Parallel-capable", 9.25, 6.83, 2.17, .27, size=12,
           color=SEED, bold=True, align="l")
    s.text("[S] Serial", 11.48, 6.83, 1.35, .27, size=12, bold=True, align="l")


def make_slide():
    slide = Slide()
    draw_framework(slide)
    return slide.root


def relationships(*links):
    root = ET.Element("Relationships", xmlns=PACKAGE_REL)
    for i, (kind, target) in enumerate(links, 1):
        ET.SubElement(root, "Relationship", Id=f"rId{i}", Type=f"{NS['r']}/{kind}", Target=target)
    return document(root)


def presentation_parts():
    r_id = f"{{{NS['r']}}}id"
    presentation = xml(None, "p:presentation")
    xml(xml(presentation, "p:sldMasterIdLst"), "p:sldMasterId",
        id="2147483648", **{r_id: "rId1"})
    xml(xml(presentation, "p:sldIdLst"), "p:sldId", id="256", **{r_id: "rId2"})
    xml(presentation, "p:sldSz", cx=emu(SIZE[0]), cy=emu(SIZE[1]), type="screen16x9")
    xml(presentation, "p:notesSz", cx=emu(7.5), cy=emu(10))

    master = xml(None, "p:sldMaster")
    shape_tree(xml(master, "p:cSld", name="Figure 2"))
    xml(master, "p:clrMap", bg1="lt1", tx1="dk1", bg2="lt2", tx2="dk2",
        accent1="accent1", accent2="accent2", accent3="accent3", accent4="accent4",
        accent5="accent5", accent6="accent6", hlink="hlink", folHlink="folHlink")
    xml(xml(master, "p:sldLayoutIdLst"), "p:sldLayoutId",
        id="2147483649", **{r_id: "rId1"})
    xml(master, "p:txStyles")

    layout = xml(None, "p:sldLayout", type="blank", preserve="1")
    shape_tree(xml(layout, "p:cSld", name="Blank"))
    xml(xml(layout, "p:clrMapOvr"), "a:masterClrMapping")

    theme = xml(None, "a:theme", name="Trussness framework")
    elements = xml(theme, "a:themeElements")
    scheme = xml(elements, "a:clrScheme", name="Framework")
    palette = dict(dk1=INK, lt1=WHITE, dk2=CONTEXT, lt2=PANEL,
                   accent1=SEED, accent2=CLOSURE, accent3=DELETION, accent4=FRAME,
                   accent5=CONTEXT, accent6=HEADER, hlink=SEED, folHlink=CLOSURE)
    for role, color in palette.items():
        fill_color = xml(scheme, f"a:{role}")
        xml(fill_color, "a:srgbClr", val=color)
    font_scheme = xml(elements, "a:fontScheme", name=FONT)
    for role in ("major", "minor"):
        family = xml(font_scheme, f"a:{role}Font")
        xml(family, "a:latin", typeface=FONT)
        xml(family, "a:ea", typeface="")
        xml(family, "a:cs", typeface="")
    fmt = xml(elements, "a:fmtScheme", name="Framework")
    style_lists = [
        xml(fmt, f"a:{tag}") for tag in ("fillStyleLst", "lnStyleLst", "effectStyleLst", "bgFillStyleLst")
    ]
    for width in ("6350", "12700", "19050"):
        xml(xml(style_lists[0], "a:solidFill"), "a:schemeClr", val="phClr")
        style = xml(style_lists[1], "a:ln", w=width, cap="flat", cmpd="sng", algn="ctr")
        xml(xml(style, "a:solidFill"), "a:schemeClr", val="phClr")
        xml(xml(style_lists[2], "a:effectStyle"), "a:effectLst")
        xml(xml(style_lists[3], "a:solidFill"), "a:schemeClr", val="phClr")

    return {
        "ppt/presentation.xml": (presentation, CONTENT_TYPE + "presentation.main+xml"),
        "ppt/slideMasters/slideMaster1.xml": (master, CONTENT_TYPE + "slideMaster+xml"),
        "ppt/slideLayouts/slideLayout1.xml": (layout, CONTENT_TYPE + "slideLayout+xml"),
        "ppt/slides/slide1.xml": (make_slide(), CONTENT_TYPE + "slide+xml"),
        "ppt/theme/theme1.xml": (theme, "application/vnd.openxmlformats-officedocument.theme+xml"),
    }


def write_pptx(output):
    parts = presentation_parts()
    mappings = [
        ("_rels/.rels", (("officeDocument", "ppt/presentation.xml"),)),
        ("ppt/_rels/presentation.xml.rels",
         (("slideMaster", "slideMasters/slideMaster1.xml"), ("slide", "slides/slide1.xml"))),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels",
         (("slideLayout", "../slideLayouts/slideLayout1.xml"), ("theme", "../theme/theme1.xml"))),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels",
         (("slideMaster", "../slideMasters/slideMaster1.xml"),)),
        ("ppt/slides/_rels/slide1.xml.rels",
         (("slideLayout", "../slideLayouts/slideLayout1.xml"),)),
    ]
    content = ET.Element("Types", xmlns=PACKAGE_TYPES)
    ET.SubElement(content, "Default", Extension="xml", ContentType="application/xml")
    ET.SubElement(content, "Default", Extension="rels",
                  ContentType="application/vnd.openxmlformats-package.relationships+xml")
    for name, (_, mime) in parts.items():
        ET.SubElement(content, "Override", PartName="/" + name, ContentType=mime)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as deck:
        deck.writestr("[Content_Types].xml", document(content))
        for name, (body, _) in parts.items():
            deck.writestr(name, document(body))
        for name, targets in mappings:
            deck.writestr(name, relationships(*targets))


def write_pdf(output):
    slide = VectorSlide()
    draw_framework(slide, title=False)
    with TemporaryDirectory(prefix="vldb-framework-") as work:
        temp = Path(work)
        source = temp / "diagram.tex"
        source.write_text(slide.tex(), encoding="utf-8")
        result = run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", source.name],
                     cwd=temp, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        output.parent.mkdir(parents=True, exist_ok=True)
        copyfile(temp / "diagram.pdf", output)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=root / "VLDB2027_Work1-main/paper/figures/figure2-fused-pruned.pptx")
    parser.add_argument("--pdf-output", type=Path, help="write a cropped vector PDF via pdflatex")
    args = parser.parse_args()
    write_pptx(args.output)
    print(args.output.resolve())
    if args.pdf_output is not None:
        write_pdf(args.pdf_output)
        print(args.pdf_output.resolve())


if __name__ == "__main__":
    main()
