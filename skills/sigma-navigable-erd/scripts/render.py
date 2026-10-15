#!/usr/bin/env python3
"""Render a blame result (from blame.trace_workbook) as one self-contained HTML page."""
import collections, html, json, os, re

NOOWNER = "Warehouse table (owner not set)"


def render_html(d, title=None):
    objs = {o["id"]: o for o in d["objects"]}
    wb = d["workbook"]
    cols = [(e, c) for e in d["elements"] for c in e["columns"]]

    # ---- per-object counts of columns where it applies logic -------------------
    logic = collections.defaultdict(int)
    for e, c in cols:
        for o in {t["obj"] for t in c["touched"]}:
            logic[o] += 1
    elem_by_owner = collections.defaultdict(set)
    col_by_owner = collections.Counter()
    for e, c in cols:
        for o in c["owners"]:
            elem_by_owner[o].add(e["name"]); col_by_owner[o] += 1

    # ---- ERD layout ------------------------------------------------------------
    level = {wb["id"]: 0}
    changed = True
    while changed:
        changed = False
        for ed in d["edges"]:
            if ed["type"] == "data-model" and ed["from"] in level:
                nl = level[ed["from"]] + 1
                if level.get(ed["to"], -1) < nl:
                    level[ed["to"]] = nl; changed = True
    for o in objs:
        level.setdefault(o, 1)
    maxl = max(level.values())
    term = collections.OrderedDict()
    tedges = collections.defaultdict(set)
    for ed in d["edges"]:
        if ed["type"] == "data-model":
            continue
        if ed["type"] == "table":
            k = ed.get("team", NOOWNER)
        elif ed["type"] == "customSQL":
            k = "Custom SQL / write-back tables"
        elif ed["type"] == "csv-upload":
            k = "CSV upload"
        else:
            k = ed["type"]
        term.setdefault(k, 0); term[k] += 1
        tedges[(ed["from"], k)].add(ed.get("to"))

    NW, NH, GX, GY = 250, 74, 90, 22
    pos = {}
    bylevel = collections.defaultdict(list)
    for o, l in level.items():
        bylevel[l].append(o)
    for l, os_ in bylevel.items():
        for i, o in enumerate(sorted(os_, key=lambda x: objs[x]["name"])):
            pos[o] = (20 + l * (NW + GX), 30 + i * (NH + GY))
    tx = 20 + (maxl + 1) * (NW + GX)
    for i, k in enumerate(term):
        pos["T:" + k] = (tx, 30 + i * (NH + GY))
    W = tx + NW + 20
    Hh = max(max(p[1] for p in pos.values()) + NH + 20, 200)


    def esc(s):
        return html.escape(str(s))


    svg = [f'<svg viewBox="0 0 {W} {Hh}" role="img" aria-label="Object lineage">']
    for ed in d["edges"]:
        if ed["type"] == "data-model" and ed["from"] in pos and ed["to"] in pos:
            a, b = pos[ed["from"]], pos[ed["to"]]
            x1, y1, x2, y2 = a[0] + NW, a[1] + NH / 2, b[0], b[1] + NH / 2
            svg.append(f'<path class="e m" d="M{x1},{y1} C{x1+GX/2},{y1} {x2-GX/2},{y2} {x2},{y2}"/>')
    for (o, k), tos in tedges.items():
        a, b = pos[o], pos["T:" + k]
        x1, y1, x2, y2 = a[0] + NW, a[1] + NH / 2, b[0], b[1] + NH / 2
        svg.append(f'<path class="e t" d="M{x1},{y1} C{x1+80},{y1} {x2-80},{y2} {x2},{y2}"><title>{esc(len(tos))} source(s)</title></path>')
    for o, ob in objs.items():
        x, y = pos[o]
        cls = "wb" if o == wb["id"] else "dm"
        sub = f"owner {ob['owner']}" + (f" · last edit {ob['editor']}" if ob["editor"] != ob["owner"] else "")
        kindtxt = "WORKBOOK" if o == wb["id"] else "DATA MODEL"
        svg.append(f'<g class="n {cls}"><rect x="{x}" y="{y}" width="{NW}" height="{NH}" rx="4"/>'
                   f'<text x="{x+10}" y="{y+16}" class="k">{kindtxt}</text>'
                   f'<text x="{x+10}" y="{y+34}" class="t">{esc(ob["name"][:34])}</text>'
                   f'<text x="{x+10}" y="{y+50}" class="o">{esc(sub[:40])}</text>'
                   f'<text x="{x+10}" y="{y+65}" class="s">logic in {logic.get(ob["name"],0)} columns</text></g>')
    tcls = lambda k: "un" if k in (NOOWNER, "Unknown") else "dp"
    for k, n in term.items():
        x, y = pos["T:" + k]
        svg.append(f'<g class="n {tcls(k)}"><rect x="{x}" y="{y}" width="{NW}" height="{NH}" rx="4"/>'
                   f'<text x="{x+10}" y="{y+16}" class="k">WAREHOUSE / SOURCE</text>'
                   f'<text x="{x+10}" y="{y+36}" class="t">{esc(k[:34])}</text>'
                   f'<text x="{x+10}" y="{y+54}" class="s">{n} source link(s)</text></g>')
    svg.append("</svg>")

    # ---- roster ---------------------------------------------------------------
    roster = []
    for o, ob in objs.items():
        roster.append((ob["owner"], ob["kind"], ob["name"], ob["editor"], logic.get(ob["name"], 0), ob["url"]))
    rows = "".join(
        f'<tr><td>{esc(r[0])}</td><td>{"Workbook" if r[1]=="workbook" else "Data model"}</td>'
        f'<td>{"<a href=%s>%s</a>" % (chr(34)+esc(r[5])+chr(34), esc(r[2])) if r[5] else esc(r[2])}</td>'
        f'<td>{esc(r[3]) if r[3]!=r[0] else "same"}</td><td class="n">{r[4]}</td></tr>'
        for r in sorted(roster, key=lambda r: (-r[4], r[0])))

    # ---- column graph (compact) ------------------------------------------------
    import re
    Nz = lambda x: re.sub(r"\s+", " ", str(x).replace("\xa0", " ")).strip()
    g = d["graph"]
    keys = list(g)
    gidx = {k: i for i, k in enumerate(keys)}
    STR, sidx = [], {}


    def si(x):
        if x is None:
            return -1
        if x not in sidx:
            sidx[x] = len(STR); STR.append(x)
        return sidx[x]


    KIND = {"col": 0, "wh": 1, "sql": 2, "input": 3, "hidden": 4, "other": 5, "manual": 6}
    gnodes = [[KIND[n["k"]], si(n["o"]), si(n["e"]), si(n["c"]), int(n["raw"]), si(n["f"]), si(n["w"]), si(n["u"]),
               [gidx[u] for u in n["ups"]], int(n.get("hid", 0)), si(n["ok"])] for n in g.values()]
    gjs = json.dumps({"S": STR, "N": gnodes, "wb": wb["name"]}, separators=(",", ":")).replace("</", "<\\/")

    # ---- recommendations ---------------------------------------------------
    recs = d.get("recommendations", [])
    rjs = json.dumps([{"i": r["id"], "t": r["type"], "s": r["sev"], "h": r["title"], "d": r["detail"], "a": r["action"], "p": r.get("verdict") or "", "q": r.get("confidence") or "",
                       "x": r.get("evidence") or "", "y": r.get("verify") or "",
                       "n": [gidx[k] for k in r["nodes"] if k in gidx], "r": {str(gidx[k]): v for k, v in r.get("roles", {}).items() if k in gidx}, "o": r["owners"], "w": r["where"], "v": r["saves"]}
                      for r in recs], separators=(",", ":")).replace("</", "<\\/")

    perf = d.get("perf") or {"available": False, "elements": [], "totals": {}, "verdicts": {}, "top": []}
    pfjs = json.dumps({**perf, "note": d.get("compiled_note", "")}, separators=(",", ":")).replace("</", "<\\/")

    # ---- compact data for the explorer ----------------------------------------
    data = []
    for e in d["elements"]:
        cc = []
        for c in e["columns"]:
            cc.append([c["name"], int(c["hidden"]), int(c["raw"]), int(c["partial"]), c["origins"][:3], c["teams"],
                       c["maybe"][:4], [[t["obj"], t["element"], t["owner"], t["formula"]] for t in c["touched"][:5]],
                       c["owners"], c["formula"][:160],
                       gidx.get(f"c|{wb['id']}|{e['id']}|{Nz(c['name'])}", -1)])
        data.append({"n": e["name"], "k": e["kind"], "c": cc})
    data.sort(key=lambda x: x["n"].lower())
    js = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")

    total = len(cols)
    nraw = sum(1 for e, c in cols if c["raw"])
    npart = sum(1 for e, c in cols if c["partial"])
    people = sorted({o["owner"] for o in objs.values()} | {t["owner"] for e, c in cols for t in c["touched"]} | {w for r in recs for w in r["owners"]})

    TEMPLATE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")).read()
    out = (TEMPLATE.replace("@@TITLE@@", esc(wb["name"])).replace("@@PTITLE@@", esc(title or "Navigable Entity Relationship Diagram")).replace("@@SVG@@", "".join(svg)).replace("@@ROWS@@", rows)
           .replace("@@DATA@@", js).replace("@@RECS@@", rjs).replace("@@PERF@@", pfjs).replace("@@NREC@@", str(len(recs))).replace("@@GRAPH@@", gjs).replace("@@NEL@@", str(len(d["elements"]))).replace("@@NCOL@@", f"{total:,}")
           .replace("@@NRAW@@", f"{nraw:,}").replace("@@NCALC@@", f"{total-nraw:,}").replace("@@NPART@@", f"{npart:,}")
           .replace("@@NOBJ@@", str(len(objs))).replace("@@WBURL@@", esc(wb["url"] or "#"))
           .replace("@@PEOPLE@@", "".join(f'<option>{esc(p)}</option>' for p in people)))


    return out
