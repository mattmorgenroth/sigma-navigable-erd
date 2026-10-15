#!/usr/bin/env python3
"""Column-level lineage and ownership tracer for a Sigma workbook (library; use run.py).

Follows each column's formula through workbook elements and data models down to the
warehouse table. Records every place logic is applied (anything that is not a plain
[Col] reference) and who owns that object. Read-only: only GET requests are made.
"""
import collections, hashlib, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
import yaml

ALLOWED_HOST_SUFFIXES = (".sigmacomputing.com",)
NOOWNER = "Warehouse table (owner not set)"
_tok = None
BASE = None


def configure(base_url):
    """Set the API base URL after checking it is a Sigma host (credentials are only ever sent there)."""
    global BASE
    u = urllib.parse.urlparse(base_url.rstrip("/"))
    if u.scheme != "https" or not (u.hostname or "").endswith(ALLOWED_HOST_SUFFIXES):
        raise SystemExit(f"Refusing base URL {base_url!r}: it must be an https Sigma API host.")
    BASE = base_url.rstrip("/")


def http(path, data=None, headers=None, tries=4):
    req = urllib.request.Request(BASE + path, data=data, headers=headers or {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise


def token():
    global _tok
    if not _tok:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": os.environ["SIGMA_CLIENT_ID"],
            "client_secret": os.environ["SIGMA_CLIENT_SECRET"]}).encode()
        _tok = json.loads(http("/v2/auth/token", body))["access_token"]
    return _tok


def api(path):
    return http(path, headers={"Authorization": "Bearer " + token()})


def load_yaml(txt):
    return yaml.safe_load(txt.replace("\t", " "))


# ---- owners ---------------------------------------------------------------
_members = {}


def member(mid):
    if mid not in _members:
        try:
            m = json.loads(api("/v2/members/" + mid))
            _members[mid] = (m.get("firstName", "") + " " + m.get("lastName", "")).strip() or m.get("email", mid)
        except Exception:
            _members[mid] = mid
    return _members[mid]


def warehouse_owner(path, overrides):
    """Owner for a warehouse table: longest matching dotted prefix in the owners file, else NOOWNER."""
    p = ".".join(path).upper()
    best = None
    for k, v in overrides.items():
        if p.startswith(k.upper()) and (best is None or len(k) > len(best[0])):
            best = (k, v)
    return best[1] if best else NOOWNER


# ---- objects --------------------------------------------------------------
TOKEN = re.compile(r"(?<![\w-])[\w-]{6,40}(?![\w-])")


class Obj:
    def __init__(self, kind, oid, spec, meta, idcount=None):
        self.kind, self.id = kind, oid
        self.idcount = idcount or {}
        self.name = spec.get("name") or meta.get("name") or oid
        self.owner = member(meta.get("ownerId") or spec.get("ownerId") or "")
        self.last_editor = member(meta.get("updatedBy") or spec.get("updatedBy") or "")
        self.url = spec.get("url") or meta.get("url")
        doc = spec.get("document") or spec
        self.elements = {}
        els = list(doc.get("elements") or [])
        for pg in doc.get("pages") or []:
            els += list(pg.get("elements") or [])
        for e in els:
            self.elements[e["id"]] = e


OBJS = {}
WH_PATHS = {}   # table name -> path, learned from warehouse-table sources
WH_CONN = {}    # dotted path -> connection id


def lineage(obj):
    if getattr(obj, "_lin", None) is None:
        try:
            base = "workbooks" if obj.kind == "workbook" else "dataModels"
            obj._lin = json.loads(api(f"/v2/{base}/{obj.id}/lineage")).get("entries", [])
        except Exception:
            obj._lin = []
    return obj._lin


NO_ACCESS = {}   # object id -> reason, for objects the API user could not read
ALLOW_FETCH = True


def get_obj(kind, oid):
    key = (kind, oid)
    if oid in NO_ACCESS:
        return None
    if key not in OBJS:
        if not ALLOW_FETCH:
            return None
        try:
            _fetch_obj(kind, oid, key)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404) and kind != "workbook":
                NO_ACCESS[oid] = f"HTTP {e.code}"
                return None
            raise
    return OBJS[key]


def _fetch_obj(kind, oid, key):
    base = "workbooks" if kind == "workbook" else "dataModels"
    txt = api(f"/v2/{base}/{oid}/spec")
    spec = load_yaml(txt)
    meta = json.loads(api(f"/v2/{base}/{oid}"))
    OBJS[key] = Obj(kind, oid, spec, meta, collections.Counter(TOKEN.findall(txt)))


def nm(x):
    if isinstance(x, dict):
        x = x.get('text') or x.get('value') or x.get('title')
        if not x:
            return None
    return x if x is None else str(x)


def N(x):
    return re.sub(r'\s+', ' ', str(x).replace('\xa0', ' ')).strip()


def colname(c):
    if c.get("name"):
        return nm(c["name"])
    m = re.fullmatch(r"\s*\[(.+)\]\s*", c.get("formula") or "")
    return m.group(1).split("/")[-1] if m else c["id"]


REF = re.compile(r"\[([^\[\]]+)\]")


class Tracer:
    def __init__(self, overrides):
        self.memo, self.overrides = {}, overrides
        self.G = {}   # column graph: node id -> {k,o,e,c,f,w,u,ups,...}

    @staticmethod
    def eurl(o, eid):
        """Link that opens the object in edit mode, at the element when one is given: <object url>/edit?:nodeId=<id>."""
        if not o.url:
            return None
        base = o.url.split("?")[0].rstrip("/")
        if not base.endswith("/edit"):
            base += "/edit"
        return base + ("?:nodeId=" + eid if eid else "")

    # sources of an element -> list of (label, node) ; node = ('el',obj,eid) | ('wh',path) | ('input',obj,eid) | ('sql',stmt) | ('manual',)
    def sources(self, obj, el):
        out = []

        def add(s):
            if not isinstance(s, dict):
                return
            k = s.get("kind")
            if k == "table" and s.get("elementId") in obj.elements:
                t = obj.elements[s["elementId"]]
                out.append((nm(t.get("name")) or t["id"], ("el", obj, t["id"])))
            elif k == "table":
                out.append(("(hidden upstream)", ("hidden", obj)))
            elif k == "data-model":
                dm = get_obj("data-model", s["dataModelId"])
                t = dm.elements.get(s["elementId"]) if dm else None
                if dm is None:
                    out.append(("(unreadable data model)", ("hidden", obj)))
                if t:
                    out.append((nm(t.get("name")) or t["id"], ("el", dm, t["id"])))
            elif k == "warehouse-table":
                WH_PATHS[s["path"][-1].upper()] = tuple(s["path"])
                WH_CONN[".".join(s["path"])] = s.get("connectionId")
                out.append((s["path"][-1], ("wh", tuple(s["path"]))))
            elif k == "sql":
                out.append((nm(el.get("name")) or "SQL", ("sql", s.get("statement", ""))))
            elif k == "join":
                add(s.get("primarySource"))
                for j in s.get("joins", []):
                    add(j.get("left")); add(j.get("right"))
            elif k in ("union", "transpose"):
                for x in s.get("sources", []):
                    add(x)
                add(s.get("primarySource"))
            elif k in ("empty", "linked"):
                out.append((nm(el.get("name")) or "Input table", ("input", obj, el["id"])))
            elif k in ("manual", "source", "upload"):
                out.append((nm(el.get("name")) or k, ("manual",)))
        add(el.get("source"))
        return out

    def trace(self, obj, eid, cname, stack=()):
        key = (obj.kind, obj.id, eid, cname)
        if key in self.memo:
            return self.memo[key]
        if key in stack:
            return self.res()
        el = obj.elements.get(eid)
        r = self.res()
        if el is None:
            r["unresolved"] = True
            return r
        col = next((c for c in el.get("columns", []) if N(colname(c)) == N(cname)), None)
        srcs = self.sources(obj, el)
        if col is None:
            # not a computed column here: it comes straight from the source(s)
            r = self.from_sources(obj, el, srcs, cname, None, stack + (key,))
            self.memo[key] = r
            return r
        f = (col.get("formula") or "").strip()
        refs = REF.findall(f)
        plain = len(refs) == 1 and re.fullmatch(r"\[[^\[\]]+\]", f) is not None
        parts = []
        for ref in refs:
            parts.append(self.resolve_ref(obj, el, srcs, ref, stack + (key,), cname))
        r = self.res()
        for p in parts:
            self.merge(r, p)
        if not plain:
            r["raw"] = False
            if not refs or all(p["unresolved"] and not p["origins"] for p in parts):
                pass
            r["touched"] = [{"obj": obj.name, "kind": obj.kind, "element": nm(el.get("name")) or eid,
                             "owner": obj.owner, "editor": obj.last_editor, "formula": f[:240]}] + r["touched"]
        if not refs and f:
            r["origins"].add("(constant / no column reference)")
        ref = f"c|{obj.id}|{eid}|{N(cname)}"
        ups = []
        for p in parts:
            x = p.get("ref")
            if x and x != ref and x not in ups:
                ups.append(x)
        self.G[ref] = {"k": "col", "ok": obj.kind, "o": obj.name, "oid": obj.id, "e": nm(el.get("name")) or "(untitled) " + eid,
                       "eid": eid, "c": colname(col), "raw": plain, "f": f[:140], "w": obj.owner,
                       "u": self.eurl(obj, eid), "ups": ups, "hid": bool(col.get("hidden")),
                       "cid": col.get("id"), "idrefs": max(obj.idcount.get(col.get("id"), 1) - 1, 0)}
        r["ref"] = ref
        self.memo[key] = r
        return r

    def resolve_ref(self, obj, el, srcs, ref, stack, self_name):
        # prefix match against source labels (all sources sharing the label are tried)
        rN = N(ref)
        cands = [(l, n) for l, n in sorted(srcs, key=lambda x: -len(N(x[0]))) if rN.startswith(N(l) + "/")]
        if cands:
            best = max(len(N(l)) for l, _ in cands)
            fallback = None
            for l, n in cands:
                if len(N(l)) != best:
                    continue
                x = self.node_col(obj, n, rN[len(N(l)) + 1:], stack)
                if x is not None and not x["unresolved"]:
                    return x
                fallback = fallback or x
            if fallback is not None:
                return fallback
        # another element in the same object (e.g. Lookup([Other Table/Col], ...))
        byname = getattr(obj, "_byname", None)
        if byname is None:
            byname = obj._byname = {}
            for x in obj.elements.values():
                n = N(nm(x.get("name")) or "")
                if n and x.get("columns"):
                    byname.setdefault(n, []).append(x["id"])
        for n in sorted(byname, key=lambda z: -len(z)):
            if rN.startswith(n + "/"):
                for xid in byname[n]:
                    if xid != el["id"]:
                        x = self.trace(obj, xid, rN[len(n) + 1:], stack)
                        if x is not None and not x["unresolved"]:
                            return x
        fb = self.lineage_ref(obj, rN, stack)
        if fb is not None:
            return fb
        # sibling column in same element
        if any(N(colname(c)) == rN for c in el.get("columns", [])) and rN != N(self_name):
            return self.trace(obj, el["id"], ref, stack)
        # bare name: look in sources
        return self.from_sources(obj, el, srcs, ref, None, stack)

    def lineage_ref(self, obj, rN, stack):
        """Source element is missing from the spec: resolve '[Upstream Name/Col]' via the object's lineage list."""
        best = None
        for e in lineage(obj):
            n = N(e.get("name") or "")
            if n and rN.startswith(n + "/") and (best is None or len(n) > len(N(best["name"]))):
                best = e
        if not best:
            return None
        col = rN[len(N(best["name"])) + 1:]
        t = best.get("type")
        if t == "data-model":
            dm = get_obj("data-model", best["dataModelId"])
            if dm is None:
                return None
            tel = next((x for x in dm.elements.values() if N(x.get("name") or "") == N(best["name"])), None)
            if tel is not None:
                return self.trace(dm, tel["id"], col, stack)
            r = self.res(); r["raw"] = False
            r["origins"].add(f"data model '{dm.name}' / {best['name']} / {col}")
            r["touched"] = [{"obj": dm.name, "kind": "data-model", "element": best["name"], "owner": dm.owner,
                             "editor": dm.last_editor, "formula": "(element not exposed by API)"}]
            r["ref"] = f"x|{dm.id}|{N(best['name'])}|{N(col)}"
            self.G[r["ref"]] = {"k": "hidden", "ok": "data-model", "o": dm.name, "oid": dm.id, "e": best["name"] + " (not exposed by API)",
                                "eid": None, "c": col, "raw": False, "f": "", "w": dm.owner, "u": Tracer.eurl(dm, None), "ups": []}
            return r
        if t == "table":
            path = WH_PATHS.get(N(best["name"]).upper())
            return self.node_col(obj, ("wh", path or (best["name"],)), col, stack)
        r = self.res()
        r["origins"].add(f"{t}: {best['name']} / {col}")
        r["terminals"].add(f"{obj.owner} ({t} in {obj.name})")
        r["ref"] = f"o|{obj.id}|{t}|{N(best['name'])}|{N(col)}"
        self.G[r["ref"]] = {"k": "other", "ok": obj.kind, "o": obj.name, "oid": obj.id, "e": f"{t}: {best['name']}", "eid": None,
                            "c": col, "raw": True, "f": "", "w": obj.owner, "u": Tracer.eurl(obj, None), "ups": []}
        return r

    def from_sources(self, obj, el, srcs, cname, _, stack):
        r = self.res()
        found = False
        for label, node in srcs:
            x = self.node_col(obj, node, cname, stack, soft=len(srcs) > 1)
            if x is not None:
                self.merge(r, x); found = True
                if not r.get("ref"):
                    r["ref"] = x.get("ref")
                if node[0] in ("el", "wh", "input", "sql", "manual"):
                    break
        if not found:
            r["unresolved"] = True
        return r

    def node_col(self, obj, node, cname, stack, soft=False):
        k = node[0]
        if k == "el":
            o, eid = node[1], node[2]
            tel = o.elements.get(eid)
            has = tel and any(N(colname(c)) == N(cname) for c in tel.get("columns", []))
            if soft and not has:
                # column may pass through from that element's own source
                subs = self.sources(o, tel) if tel else []
                if not subs:
                    return None
            return self.trace(o, eid, cname, stack)
        r = self.res()
        if k == "hidden":
            o = node[1]
            r["origins"].add(f"upstream of '{o.name}' (element not exposed by the API)")
            r["terminals"].add(f"{o.owner} (data model: {o.name})")
            r["partial"] = True
            r["ref"] = f"h|{o.id}|{N(cname)}"
            self.G[r["ref"]] = {"k": "hidden", "ok": o.kind, "o": o.name, "oid": o.id,
                                "e": "(upstream element not exposed by the API)", "eid": None, "c": cname, "raw": False,
                                "f": "", "w": o.owner, "u": Tracer.eurl(o, None), "ups": []}
            for e in lineage(o):
                if e.get("type") == "data-model" and e.get("dataModelId") != o.id:
                    dm = get_obj("data-model", e["dataModelId"])
                    if dm:
                        r["maybe"].add(f"{dm.owner} (data model: {dm.name})")
            return r
        if k == "wh":
            r["origins"].add(".".join(node[1]) + "." + cname)
            team = warehouse_owner(node[1], self.overrides)
            r["terminals"].add(team)
            r["ref"] = f"w|{'.'.join(node[1])}|{N(cname)}"
            self.G[r["ref"]] = {"k": "wh", "ok": "warehouse", "o": ".".join(node[1][:-1]), "oid": None, "e": node[1][-1],
                                "eid": None, "c": cname, "raw": True, "f": "", "w": team, "u": None, "ups": []}
        elif k == "input":
            o = node[1]
            r["origins"].add(f"input table '{o.elements[node[2]].get('name')}' ({o.name})")
            r["terminals"].add(f"{o.owner} (input table in {o.name})")
            r["raw"] = False
            r["touched"] = [{"obj": o.name, "kind": o.kind, "element": nm(o.elements[node[2]].get("name")),
                             "owner": o.owner, "editor": o.last_editor, "formula": "user-entered write-back data"}]
            r["ref"] = f"i|{o.id}|{node[2]}|{N(cname)}"
            self.G[r["ref"]] = {"k": "input", "ok": o.kind, "o": o.name, "oid": o.id,
                                "e": nm(o.elements[node[2]].get("name")) or "(input table)", "eid": node[2], "c": cname,
                                "raw": False, "f": "user-entered write-back data", "w": o.owner,
                                "u": self.eurl(o, node[2]), "ups": []}
        elif k == "sql":
            r["origins"].add("custom SQL: " + node[1][:80])
            m = re.search(r"from\s+([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)", node[1], re.I)
            team = warehouse_owner(list(m.groups()), self.overrides) if m else NOOWNER
            r["terminals"].add(team)
            h = hashlib.md5(node[1].encode()).hexdigest()[:8]
            r["ref"] = f"s|{h}|{N(cname)}"
            self.G[r["ref"]] = {"k": "sql", "ok": "sql", "o": "custom SQL", "oid": None, "e": node[1][:90], "eid": None,
                                "c": cname, "raw": True, "f": "", "w": team, "u": None, "ups": []}
        else:
            r["origins"].add("manual / control values")
            r["ref"] = f"m|{N(cname)}"
            self.G[r["ref"]] = {"k": "manual", "ok": "manual", "o": "manual values", "oid": None, "e": "control / manual list",
                                "eid": None, "c": cname, "raw": True, "f": "", "w": "", "u": None, "ups": []}
        return r

    @staticmethod
    def res():
        return {"raw": True, "origins": set(), "terminals": set(), "touched": [], "unresolved": False, "partial": False, "maybe": set()}

    @staticmethod
    def merge(a, b):
        a["raw"] = a["raw"] and b["raw"]
        a["origins"] |= b["origins"]
        a["terminals"] |= b["terminals"]
        a["unresolved"] = a["unresolved"] or b["unresolved"]
        a["partial"] = a["partial"] or b.get("partial", False)
        a["maybe"] |= b.get("maybe", set())
        seen = {(t["obj"], t["element"]) for t in a["touched"]}
        for t in b["touched"]:
            if (t["obj"], t["element"]) not in seen:
                a["touched"].append(t); seen.add((t["obj"], t["element"]))


def table_columns(conn, dotted):
    """Column names of a warehouse table (None when the API cannot resolve it)."""
    try:
        body = json.dumps({"path": dotted.split(".")}).encode()
        hit = json.loads(http(f"/v2/connection/{conn}/lookup", body,
                              {"Authorization": "Bearer " + token(), "Content-Type": "application/json"}))
        inode, names, page = hit.get("inodeId"), [], None
        while inode:
            q = "pageSize=500" + (f"&page={page}" if page else "")
            d = json.loads(api(f"/v2/connections/tables/{inode}/columns?{q}"))
            names += [c.get("name") for c in d.get("entries", []) if c.get("name")]
            page = d.get("nextPage")
            if not page:
                break
        return names
    except Exception:
        return None


def trace_workbook(wid, overrides=None):
    """Trace every column of every table-like element in workbook `wid`. Returns the result dict."""
    overrides = overrides or {}
    OBJS.clear(); WH_PATHS.clear(); WH_CONN.clear(); NO_ACCESS.clear()
    wb = get_obj("workbook", wid)
    tr = Tracer(overrides)
    elements = []
    for eid, el in wb.elements.items():
        if el.get("kind") not in ("table", "pivot-table", "bar-chart", "line-chart", "kpi", "input-table"):
            continue
        cols = []
        for c in el.get("columns", []):
            r = tr.trace(wb, eid, colname(c))
            owners = []
            for t in r["touched"]:
                if t["owner"] not in owners:
                    owners.append(t["owner"])
            cols.append({"name": colname(c), "hidden": bool(c.get("hidden")), "raw": r["raw"],
                         "origins": sorted(r["origins"])[:6], "teams": sorted(r["terminals"]),
                         "touched": r["touched"][:8], "owners": owners, "unresolved": r["unresolved"] and not r["origins"], "partial": r["partial"], "maybe": sorted(r["maybe"]),
                         "formula": (c.get("formula") or "")[:200]})
        elements.append({"id": eid, "kind": el["kind"], "name": nm(el.get("name")) or "(untitled) " + eid,
                         "source": [(l, n[0]) for l, n in tr.sources(wb, el)], "columns": cols})
    edges = []
    for o in list(OBJS.values()):
        seen = set()
        for e in lineage(o):
            t = e.get("type")
            if t == "data-model" and e.get("dataModelId") != o.id and ("dm", e["dataModelId"]) not in seen:
                seen.add(("dm", e["dataModelId"])); edges.append({"from": o.id, "to": e["dataModelId"], "type": "data-model"})
            elif t == "table":
                path = WH_PATHS.get((e.get("name") or "").upper())
                key = ("t", e.get("name"))
                if key not in seen:
                    seen.add(key); edges.append({"from": o.id, "to": e.get("name"), "type": "table",
                                                 "team": warehouse_owner(list(path), overrides) if path else NOOWNER})
            elif t in ("customSQL", "csv-upload", "dataset") and (t, e.get("name")) not in seen:
                seen.add((t, e.get("name"))); edges.append({"from": o.id, "to": e.get("name"), "type": t,
                                                            "detail": (e.get("definition") or "")[:120]})
    objs = [{"kind": o.kind, "id": o.id, "name": o.name, "owner": o.owner, "editor": o.last_editor, "url": o.url}
            for o in OBJS.values()]
    # element sources and available columns, used by the recommendation engine
    global ALLOW_FETCH
    ALLOW_FETCH = False   # index only the objects already traced; do not crawl further
    elem_meta, elem_cols, elem_src = {}, {}, {}
    for o in list(OBJS.values()):
        for eid, el in o.elements.items():
            if not el.get("columns") and not el.get("source"):
                continue
            k = f"{o.id}|{eid}"
            elem_meta[k] = {"obj": o.name, "oid": o.id, "kind": o.kind, "name": nm(el.get("name")) or "(untitled) " + eid,
                            "ek": el.get("kind"), "url": Tracer.eurl(o, eid)}
            elem_cols[k] = [colname(c) for c in el.get("columns", [])]
            ss = []
            for label, n in tr.sources(o, el):
                if n[0] == "el":
                    ss.append({"label": label, "kind": "el", "ref": f"{n[1].id}|{n[2]}"})
                elif n[0] == "wh":
                    ss.append({"label": label, "kind": "wh", "ref": ".".join(n[1])})
                else:
                    ss.append({"label": label, "kind": n[0], "ref": ""})
            elem_src[k] = ss
    ALLOW_FETCH = True
    table_cols = {}
    for dotted, conn in WH_CONN.items():
        names = table_columns(conn, dotted) if conn else None
        if names:
            table_cols[dotted] = names
    return {"workbook": {"id": wid, "name": wb.name, "owner": wb.owner, "url": wb.url},
            "objects": objs, "edges": edges, "elements": elements, "graph": tr.G,
            "no_access": sorted(NO_ACCESS), "elem_meta": elem_meta, "elem_cols": elem_cols, "elem_src": elem_src, "table_cols": table_cols}
