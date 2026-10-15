#!/usr/bin/env python3
"""Recommendation engine: surface logic in a traced workbook that is duplicated and could be removed.

Input is the dict returned by blame.trace_workbook. Output is a list of recommendations, each:
  {id, type, sev, title, detail, action, nodes[], owners[], where[], saves}
Detectors (all heuristic; each recommendation says what to verify):
  lookup        a column pulled with Lookup() that is already available from the element's own sources
  multi-lookup  several lookups to the same source on the same key (a join or relationship would replace them)
  dup-formula   the same formula written in more than one place
  dup-element   two elements that share most of their columns and formulas
  dead-column   hidden calculated columns that nothing references
"""
import collections, itertools, re
import types

import perf

NOOWNER = "Warehouse table (owner not set)"
LOOKUP = re.compile(r"^\s*Lookup\(\s*(\[[^\]]+\])\s*,\s*(\[[^\]]+\](?:\s*,\s*\[[^\]]+\])*)\s*\)\s*$", re.I | re.S)
REFS = re.compile(r"\[([^\[\]]+)\]")


def N(x):
    return re.sub(r"\s+", " ", str(x).replace("\xa0", " ")).strip()


def nk(x):
    """Name key that matches warehouse names (ORGANIZATION_UUID) to Sigma display names (Organization Uuid)."""
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def split_ref(ref):
    """'[Source/Col]' -> ('Source', 'Col'); '[Col]' -> (None, 'Col')."""
    inner = ref.strip()[1:-1]
    if "/" in inner:
        a, b = inner.rsplit("/", 1)
        return N(a), N(b)
    return None, N(inner)


def recommend(d):
    g = d["graph"]
    elem_cols, elem_src, elem_meta, table_cols = d["elem_cols"], d["elem_src"], d["elem_meta"], d["table_cols"]
    objowner = {o["id"]: o["owner"] for o in d["objects"]}
    wb_name = d["workbook"]["name"]
    recs = []

    def ek_of(key):
        p = key.split("|")
        return f"{p[1]}|{p[2]}"

    def ename(ek):
        m = elem_meta.get(ek, {})
        name = re.sub(r"\s+", " ", m.get("name", ek).replace("\xa0", " ")).strip()
        if name.startswith("(untitled)"):
            first = (elem_src.get(ek) or [{}])[0].get("label")
            name = f"{first} table ({ek.split('|')[-1]})" if first else name
        return name, m.get("obj", "")

    def flabel(s):
        """Readable label for a source (unnamed elements fall back to their own first source)."""
        l = s["label"]
        if s["kind"] == "el" and l == s["ref"].split("|")[-1]:
            return ename(s["ref"])[0]
        return l

    col_nodes = {k: n for k, n in g.items() if n["k"] == "col"}
    rev = collections.defaultdict(list)          # node -> nodes that reference it
    for k_, n_ in g.items():
        for u_ in n_["ups"]:
            rev[u_].append(k_)
    by_elem = collections.defaultdict(list)
    for k, n in col_nodes.items():
        by_elem[ek_of(k)].append(k)

    # terminals (warehouse / sql / input origins) of a node, memoized
    memo = {}

    def terminals(k, stack=()):
        if k in memo:
            return memo[k]
        n = g[k]
        if n["k"] != "col":
            return {k}
        if k in stack:
            return set()
        out = set()
        for u in n["ups"]:
            out |= terminals(u, stack + (k,))
        memo[k] = out
        return out

    # columns reachable from an element's sources, shallowest first
    def available(ek, maxd=6, exclude=()):
        out, frontier, seen = {}, [(ek, 0)], {ek} | set(exclude)
        while frontier:
            cur, dep = frontier.pop(0)
            if dep >= maxd:
                continue
            for s in elem_src.get(cur, []):
                if s["ref"] in exclude and cur == ek:
                    continue
                if s["kind"] == "el" and s["ref"] in elem_cols:
                    for nm_ in elem_cols[s["ref"]]:
                        out.setdefault(nk(nm_), (dep + 1, flabel(s), "el", s["ref"], nm_))
                    if s["ref"] not in seen:
                        seen.add(s["ref"]); frontier.append((s["ref"], dep + 1))
                elif s["kind"] == "wh":
                    for nm_ in table_cols.get(s["ref"], []):
                        out.setdefault(nk(nm_), (dep + 1, flabel(s), "wh", s["ref"], nm_))
        return out

    def wh_node(ref, col_name):
        """Graph node for a warehouse column. Added on demand, because a column nothing references is not in the traced graph."""
        key = f"w|{ref}|{N(col_name)}"
        if key not in g:
            parts = ref.split(".")
            team = next((n_["w"] for n_ in g.values() if n_["k"] == "wh" and n_["o"] == ".".join(parts[:-1]) and n_["e"] == parts[-1]), NOOWNER)
            g[key] = {"k": "wh", "ok": "warehouse", "o": ".".join(parts[:-1]), "oid": None, "e": parts[-1], "eid": None,
                      "c": col_name, "raw": True, "f": "", "w": team, "u": None, "ups": []}
        return key

    def owners_of(keys, extra=()):
        seen, out = set(), []
        for k in keys:
            w = g[k]["w"] if k in g else ""
            if w and w != NOOWNER and w not in seen:      # a placeholder is not someone to invite
                seen.add(w); out.append(w)
        for w in extra:
            if w and w != NOOWNER and w not in seen:
                seen.add(w); out.append(w)
        return out

    def add(type_, sev, title, detail, action, nodes, saves, where=(), extra_owners=(), roles=None,
            verdict=None, confidence=None, evidence="", verify="", impact=None):
        nodes = list(dict.fromkeys(nodes))[:16]
        recs.append({"verdict": verdict, "confidence": confidence, "evidence": evidence, "verify": verify, "impact": impact, **{"id": f"r{len(recs)+1}", "type": type_, "sev": sev, "title": title, "detail": detail,
                     "action": action, "nodes": nodes, "owners": owners_of(nodes, extra_owners),
                     "where": list(dict.fromkeys(where))[:6], "saves": saves,
                     "roles": {k: v for k, v in (roles or {}).items() if k in nodes}}})

    # ---- 1. redundant lookup ---------------------------------------------
    lookups = []   # (key, node, srcLabel, valName, keyRefs)
    avail_cache = {}
    for k, n in col_nodes.items():
        f = N(n["f"] or "")
        m = LOOKUP.match(f)
        if not m or n["raw"]:
            continue
        slabel, vname = split_ref(m.group(1))
        keyrefs = tuple(N(x) for x in REFS.findall(m.group(2)))
        lookups.append((k, n, slabel, vname, keyrefs))
        ek = ek_of(k)
        if ek not in avail_cache:
            avail_cache[ek] = available(ek)
        hit = avail_cache[ek].get(nk(vname))
        if not hit:
            continue
        dep, label, kind, ref, hname = hit
        # same origin? compare terminals of the lookup column with the available column
        tx = terminals(k)
        if kind == "wh":
            fk = wh_node(ref, hname)
            found_t = {fk}
        else:
            fk = f"c|{ref.split('|')[0]}|{ref.split('|')[1]}|{N(hname)}"
            found_t = terminals(fk) if fk in g else set()
        tk = lambda key: "|".join(re.sub(r"[^a-z0-9]", "", p.lower()) for p in key.split("|"))   # SEGMENT == Segment
        same = bool({tk(x) for x in found_t} & {tk(x) for x in tx}) if found_t else None
        ename_, oname = ename(ek)
        direct = dep == 1
        sev = "high" if direct and same else ("med" if direct or same else "low")
        origin_txt = ("both trace to the same warehouse column" if same else
                      "they trace to different source tables, so confirm the values match" if same is False else
                      "origin of the existing column could not be compared")
        add("lookup", sev,
            f"'{n['c']}' is looked up from {slabel or 'another element'} but already available upstream",
            f"In {ename_} ({oname}), '{n['c']}' uses Lookup([{slabel}/{vname}], …). A column named '{hname}' is already "
            f"exposed by {label} ({'direct source' if direct else f'{dep} levels upstream'}); {origin_txt}.",
            f"Reference [{label}/{hname}] directly and delete the lookup column. Check that '{hname}' is the same value "
            f"for every row before removing it.",
            [k] + ([fk] if fk in g else []) + rev.get(k, [])[:4], "1 lookup", [ename_ + " / " + oname],
            roles={**{k: "Remove this lookup"}, **({fk: "Use this column instead"} if fk in g else {}),
                   **{c_: "Re-point to the existing column" for c_ in rev.get(k, [])[:4]}})

    # ---- 1b. lookup whose value a downstream element already receives another way
    consumers = collections.defaultdict(list)
    for ek_, ss in elem_src.items():
        for s in ss:
            if s["kind"] == "el" and ek_ not in consumers[s["ref"]]:
                consumers[s["ref"]].append(ek_)
    for k_, n_ in col_nodes.items():          # elements that reference another element in a formula (e.g. Lookup)
        for u in n_["ups"]:
            if u in col_nodes and ek_of(u) != ek_of(k_) and ek_of(k_) not in consumers[ek_of(u)]:
                consumers[ek_of(u)].append(ek_of(k_))
    flagged = {r["nodes"][0] for r in recs if r["type"] == "lookup"}
    for k, n, slabel, vname, keyrefs in lookups:
        if k in flagged:
            continue
        ex = ek_of(k)
        frontier, seen, found = [(ex, 0)], {ex}, None
        while frontier and not found:
            cur, dep = frontier.pop(0)
            if dep >= 4:
                continue
            for c in consumers.get(cur, []):
                if c in seen:
                    continue
                seen.add(c)
                hit = available(c, maxd=3, exclude={cur}).get(nk(vname))
                if hit and hit[3] != ex:
                    found = (c, hit); break
                frontier.append((c, dep + 1))
        if not found:
            continue
        c, (dep, label, kind, ref, hname) = found
        ex_name, ex_obj = ename(ex)
        c_name, c_obj = ename(c)
        owners_extra = [objowner.get(c.split("|")[0])]
        cons_nodes = [x for x in rev.get(k, []) if ek_of(x) == c][:5] or [x for x in by_elem[c] if any(ek_of(u) == ex for u in g[x]["ups"])][:5]
        hk_ = wh_node(ref, hname) if kind == "wh" else f"c|{ref.split('|')[0]}|{ref.split('|')[1]}|{N(hname)}"
        have = [hk_] if hk_ in g else []
        add("lookup", "med",
            f"'{n['c']}' is looked up in {ex_name}, but {c_name} already gets it from {label}",
            f"In {ex_name} ({ex_obj}), '{n['c']}' is pulled with Lookup([{slabel}/{vname}], …). The downstream element "
            f"{c_name} ({c_obj}) also receives a column named '{hname}' through {label}"
            f"{' (a warehouse table)' if kind == 'wh' else ''}. The same value is being derived twice.",
            f"Drop the lookup from {ex_name} and use [{label}/{hname}] in {c_name}. If {ex_name} needs the value as a join "
            f"key, join on a key it already has. Confirm both columns hold the same value first (they come from different tables).",
            [k] + cons_nodes + have, "1 lookup", [f"{ex_name} ({ex_obj})", f"{c_name} ({c_obj})"], extra_owners=owners_extra,
            roles={**{k: "Remove this lookup"}, **{c_: "Uses the lookup: re-point it" for c_ in cons_nodes},
                   **{h_: "Already provides this value" for h_ in have}})

    # ---- 2. several lookups to one source on one key ----------------------
    grp = collections.defaultdict(list)
    for k, n, slabel, vname, keyrefs in lookups:
        grp[(ek_of(k), slabel, keyrefs)].append((k, vname))
    for (ek, slabel, keyrefs), items in grp.items():
        if len(items) < 3:
            continue
        ename_, oname = ename(ek)
        names = ", ".join(sorted({v for _, v in items})[:6])
        add("multi-lookup", "high" if len(items) >= 5 else "med",
            f"{len(items)} lookups to {slabel or 'one source'} on the same key in {ename_}",
            f"{len(items)} columns in {ename_} ({oname}) each run Lookup against {slabel} using the same key "
            f"({', '.join(keyrefs[:2])}). Values pulled: {names}{'…' if len(items) > 6 else ''}.",
            f"Join {slabel} once (or add it as a relationship) and reference its columns, replacing {len(items)} lookups.",
            [k for k, _ in items], f"{len(items)} lookups → 1 join", [ename_ + " / " + oname],
            roles={k: "Replace with a joined column" for k, _ in items})

    # ---- 3. same formula written more than once -----------------------------
    fgroups = collections.defaultdict(list)
    for k, n in col_nodes.items():
        f = N(n["f"] or "")
        if n["raw"] or len(f) < 25 or re.fullmatch(r"\[[^\[\]]+\]", f) or n.get("k") != "col":
            continue
        fgroups[f].append(k)

    def ancestors(ek, maxd=8):
        out, frontier, seen = {}, [(ek, 0)], {ek}
        while frontier:
            cur, dep = frontier.pop(0)
            if dep >= maxd:
                continue
            for s in elem_src.get(cur, []):
                if s["kind"] == "el" and s["ref"] not in seen:
                    seen.add(s["ref"]); out[s["ref"]] = dep + 1; frontier.append((s["ref"], dep + 1))
        return out

    for f, keys in fgroups.items():
        if len(keys) < 2:
            continue
        eks = list(dict.fromkeys(ek_of(k) for k in keys))
        # a shared upstream element where the logic could live once
        anc = [ancestors(e) for e in eks]
        common = set(anc[0]).intersection(*[set(a) for a in anc[1:]]) if anc else set()
        home = min(common, key=lambda r: sum(a.get(r, 99) for a in anc)) if common else None
        hname = ename(home) if home else None
        where = [f"{ename(e)[0]} ({ename(e)[1]})" for e in eks]
        sev = "high" if len(keys) >= 5 or len(eks) >= 4 else ("med" if len(keys) >= 3 else "low")
        n0 = g[keys[0]]
        add("dup-formula", sev,
            f"Same formula written {len(keys)} times ('{n0['c']}')",
            f"Identical logic appears in {len(eks)} element{'s' if len(eks) > 1 else ''}: {', '.join(where[:4])}"
            f"{'…' if len(where) > 4 else ''}. Formula: {f[:150]}{'…' if len(f) > 150 else ''}",
            (f"Define it once in {hname[0]} ({hname[1]}), which all of them already draw from, and reference that column."
             if hname else "Define it once in a shared upstream element or data model and reference it everywhere."),
            keys, f"{len(keys)} copies → 1", where, roles={k: "Duplicate copy: reference one definition" for k in keys})

    # ---- 4. elements that repeat the same calculated columns ------------------
    # Only calculated columns count. Elements that read the same table share pass-through column names by definition,
    # so those are not duplicated logic. Matching elements are grouped into one finding.
    sig = {}
    for ek, ks in by_elem.items():
        calc = {(nk(g[k]["c"]), N(g[k]["f"] or "")): k for k in ks if not g[k]["raw"] and g[k]["f"]}
        if len(calc) >= 5:
            sig[ek] = calc
    parent = {e: e for e in sig}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairj = {}
    for a, b in itertools.combinations(sig, 2):
        if ename(a)[0] == ename(b)[0] and a.split("|")[0] != b.split("|")[0]:
            continue   # same-named elements in different objects are usually deliberate regional copies
        sa, sb = set(sig[a]), set(sig[b])
        inter = len(sa & sb)
        if inter < 5:
            continue
        j = inter / len(sa | sb)
        if j >= 0.8:
            pairj[(a, b)] = (j, inter)
            parent[find(a)] = find(b)
    clusters = collections.defaultdict(list)
    for e in sig:
        clusters[find(e)].append(e)
    for members in clusters.values():
        if len(members) < 2:
            continue
        mset = set(members)
        ps = [v for p, v in pairj.items() if p[0] in mset and p[1] in mset]
        pct = int(min(v[0] for v in ps) * 100)
        shared = set.intersection(*[set(sig[m]) for m in members])
        if len(shared) < 5:
            shared = set(sig[members[0]]) & set(sig[members[1]])
        names = [f"{ename(m)[0]} ({ename(m)[1]})" for m in members]
        picked, roles = [], {}
        for m in members[:4]:
            for sg in sorted(shared)[:3]:
                if sg in sig[m]:
                    picked.append(sig[m][sg])
                    roles[sig[m][sg]] = f"Same logic in {len(members) - 1} other element{'s' if len(members) > 2 else ''}"
        k_ = len(members)
        title = (f"{ename(members[0])[0]} and {ename(members[1])[0]} repeat the same calculated columns" if k_ == 2
                 else f"{k_} elements repeat the same calculated columns")
        add("dup-element", "high" if k_ >= 3 or pct >= 95 else "med", title,
            f"{'; '.join(names[:5])}{'…' if k_ > 5 else ''} share at least {min(v[1] for v in ps)} calculated columns with "
            f"identical formulas (at least {pct}% overlap). Filters, sorting and layout are not compared, so they may still differ.",
            "If the elements only differ by a filter, keep one and filter it with a control or a saved view. Otherwise move the "
            "shared calculated columns into one common source and point the others at it.",
            picked, f"{k_ - 1} redundant element{'s' if k_ > 2 else ''}", names,
            extra_owners=[objowner.get(m.split("|")[0]) for m in members], roles=roles)

    # ---- 5. hidden calculated columns nothing uses --------------------------
    referenced = {u for n in g.values() for u in n["ups"]}
    dead = collections.defaultdict(list)
    for k, n in col_nodes.items():
        if n["ok"] == "workbook" and n.get("hid") and not n["raw"] and k not in referenced and n.get("idrefs", 0) == 0:
            dead[ek_of(k)].append(k)
    for ek, ks in dead.items():
        ename_, oname = ename(ek)
        names = ", ".join(g[k]["c"] for k in ks[:6])
        add("dead-column", "med" if len(ks) >= 5 else "low",
            f"{len(ks)} hidden calculated column{'s' if len(ks) > 1 else ''} in {ename_} {'are' if len(ks) > 1 else 'is'} not used",
            f"No formula, filter, sort, grouping or control in the workbook refers to: {names}{'…' if len(ks) > 6 else ''}.",
            "Delete them. First check they are not used by an action, a saved view, or something outside this workbook.",
            ks, f"{len(ks)} column{'s' if len(ks) > 1 else ''}", [ename_ + " / " + oname], roles={k: "Unused: safe to delete after checking" for k in ks})

    # ---- performance opinions: measured from the generated SQL where it is available, inferred from formulas otherwise
    ctx = types.SimpleNamespace(g=g, by_elem=by_elem, col_nodes=col_nodes, add=add, ename=ename, ek_of=ek_of, ancestors=ancestors)
    perf.add_perf_recs(d, ctx)
    perf.annotate(d, recs, ctx)

    order = {"high": 0, "med": 1, "low": 2}
    recs.sort(key=lambda r: (order[r["sev"]], r["type"]))
    for i, r in enumerate(recs):
        r["id"] = f"r{i+1}"
    d["perf"] = perf.summarize(d, recs)
    return recs


def summarize(recs):
    by = collections.Counter(r["type"] for r in recs)
    return {"total": len(recs), "by_type": dict(by), "by_sev": dict(collections.Counter(r["sev"] for r in recs))}
