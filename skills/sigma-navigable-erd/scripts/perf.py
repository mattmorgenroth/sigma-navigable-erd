#!/usr/bin/env python3
"""Performance opinions: which consolidations are likely to make the compiled SQL cheaper, and which are only tidier.

Everything here is a heuristic ranking, not a measurement of run time. Two sources of evidence are combined:

  measured   the SQL Sigma generates for each element, read from the API (GET /v2/workbooks/{id}/queries):
             joins, tables read (and read more than once), windows, regular expressions, JSON parsing, AI calls,
             lookups the compiler could not prove return one row, casts inside join conditions, and identical
             expensive calls repeated inside one query
  inferred   the workbook's formulas: Lookup() fan-out, expensive sub-expressions repeated in several columns,
             key expressions wrapped in functions

What the generated SQL shows, and what the opinions rest on:
  * Every element compiles to its own query, and that query includes everything upstream of the element. Two elements
    that sit on the same joins each pay for those joins separately. Work is shared inside one query, not between queries.
  * Inside one query, identical logic is generated once. Repeating a whole formula inside one element is usually free;
    repeating it across elements is not.
  * Columns nothing uses are left out of the SQL, so deleting them tidies the workbook without speeding up a query.
  * A Lookup() becomes a join. The more of them, the more joins, and a lookup that is not provably one row per key is
    wrapped in min/max with a null-safe comparison.
  * A cast or function wrapped around a join key hides the key from the optimizer.

Opinions carry a verdict ("Likely faster", "May be faster", "Mostly maintainability"), a confidence ("Measured" when the
SQL shows it, "Inferred" when only the formulas do), the evidence, and a way to check it.
"""
import collections
import re

# ---- weights: used only to rank elements against each other, never as seconds ---------------------------------
W_JOIN, W_EXTRA_SCAN, W_WINDOW, W_DISTINCT_AGG, W_REGEX, W_JSON, W_AI, W_GUARD = 3, 4, 2, 3, 1, 3, 50, 0.5

FAMILY_SQL = {
    "ai": re.compile(r"\b(?:snowflake\.cortex\.\w+|ai_\w+|cortex_\w+)\s*\(", re.I),
    "json": re.compile(r"\b(?:try_parse_json|parse_json|json_extract_path_text)\s*\(", re.I),
    "regex": re.compile(r"\b(?:regexp_\w+|rlike)\s*\(", re.I),
}
TABLE_REF = re.compile(r"\b(?:from|join)\s+((?:\"?[A-Za-z0-9_$]+\"?\.){2}\"?[A-Za-z0-9_$\-]+\"?)", re.I)
JOIN = re.compile(r"\bjoin\b", re.I)
WINDOW = re.compile(r"\bover\s*\(", re.I)
DISTINCT_AGG = re.compile(r"\bcount\s*\(\s*distinct\b", re.I)
FLATTEN = re.compile(r"\bflatten\s*\(", re.I)
GUARD = re.compile(r"\bequal_null\s*\(\s*min\s*\(", re.I)
ON_CLAUSE = re.compile(r"\bon\b(.{0,300}?)(?=\bjoin\b|\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\)\s*[A-Za-z_]|\Z)", re.I | re.S)

# formula-side families (Sigma function names) and how costly each is, for ranking sub-expressions
FORMULA_FAMILY = [
    ("ai", re.compile(r"\bCall[A-Z]\w*\s*\("), 50),
    ("lookup", re.compile(r"\bLookup\s*\("), 5),
    ("window", re.compile(r"\b(?:Rank\w*|RowNumber|Lag|Lead|Moving\w+|Cumulative\w+|FillDown|PercentRank|NTile)\s*\("), 3),
    ("aggregate", re.compile(r"\b(?:Sum|Count|CountDistinct|CountIf|SumIf|Avg|AvgIf|Min|Max|Median|Percentile\w*|ListAgg|StdDev\w*|Variance\w*)\s*\("), 3),
    ("regex", re.compile(r"\bRegexp\w*\s*\("), 2),
    ("json", re.compile(r"\b(?:Json\w*|Variant\w*|Parse\w*)\s*\("), 3),
]
SQL_FOR_FAMILY = {"ai": "ai", "regex": "regex", "json": "json"}      # formula family -> SQL family it can be confirmed in

VERDICTS = ("Likely faster", "May be faster", "Mostly maintainability")


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def and_list(items, limit=4):
    items = list(items)
    if len(items) > limit:
        return ", ".join(items[:limit]) + f" and {len(items) - limit} more"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def short_table(t):
    """A readable name for a table. Sigma's input tables and materialized elements have long generated names."""
    name = t.split(".")[-1].strip('"')
    m = re.match(r"(SIGDS|T_MAT)_([0-9A-Fa-f_]{12,})", name)
    if m:
        kind = "input table" if m.group(1) == "SIGDS" else "materialized element"
        return f"{kind} {m.group(1)}_{m.group(2).replace('_', '')[:6]}…"
    return name


def unique_labels(pairs):
    """[(id, name)] -> {id: label}, adding the id only where two elements share a name."""
    names = collections.Counter(n for _, n in pairs)
    return {i: (n if names[n] == 1 else f"{n} [{i}]") for i, n in pairs}


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------------------------------------------------------
# Measured: read one element's generated SQL
# ---------------------------------------------------------------------------------------------------------------
def _balanced_end(sql, open_idx):
    """Index just past the parenthesis that closes the one at open_idx, skipping quoted strings and identifiers."""
    depth, i, n = 0, open_idx, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"":
            q = c
            i += 1
            while i < n and sql[i] != q:
                i += 2 if (sql[i] == "\\" and q == "'") else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _calls(sql, pattern):
    out = []
    for m in pattern.finditer(sql):
        out.append(norm(sql[m.start():_balanced_end(sql, m.end() - 1)]).lower())
    return out


def profile_sql(sql):
    """Counts of the things that cost something, from one element's generated SQL."""
    body = sql.split("\n\n-- Sigma")[0]
    tables = collections.Counter(t.replace('"', "").upper() for t in TABLE_REF.findall(body))
    repeats = []
    for fam, pat in FAMILY_SQL.items():
        for text, n in collections.Counter(_calls(body, pat)).items():
            if n >= 2:
                repeats.append({"family": fam, "count": n, "sample": text[:110]})
    return {
        "bytes": len(body),
        "tables": dict(tables),
        "joins": len(JOIN.findall(body)),
        "windows": len(WINDOW.findall(body)),
        "distinct_aggs": len(DISTINCT_AGG.findall(body)),
        "flatten": len(FLATTEN.findall(body)),
        "guarded": len(GUARD.findall(body)),
        "cast_in_join": sum(1 for m in ON_CLAUSE.finditer(body) if ("::" in m.group(1) or re.search(r"\bcast\s*\(", m.group(1), re.I))),
        "regex": len(_calls(body, FAMILY_SQL["regex"])),
        "json": len(_calls(body, FAMILY_SQL["json"])),
        "ai": len(_calls(body, FAMILY_SQL["ai"])),
        "repeats": repeats,
    }


def cost_index(p):
    """A relative weight for ranking elements. It is not a time or a dollar figure."""
    extra_scans = sum(n - 1 for n in p["tables"].values() if n > 1)
    return round(W_JOIN * p["joins"] + W_EXTRA_SCAN * extra_scans + W_WINDOW * p["windows"] + W_DISTINCT_AGG * p["distinct_aggs"]
                 + W_REGEX * p["regex"] + W_JSON * (p["json"] + p["flatten"]) + W_AI * p["ai"] + W_GUARD * p["guarded"], 1)


# ---------------------------------------------------------------------------------------------------------------
# Inferred: look inside formulas
# ---------------------------------------------------------------------------------------------------------------
def split_calls(formula):
    """Every function call in a formula, including nested ones, as (name, text). Strings and [refs] are skipped."""
    out = []

    def scan(s):
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == '"':
                i += 1
                while i < n and s[i] != '"':
                    i += 2 if s[i] == "\\" else 1
            elif c == "[":
                j = s.find("]", i)
                i = n if j < 0 else j
            elif c.isalpha() or c == "_":
                j = i
                while j < n and (s[j].isalnum() or s[j] == "_"):
                    j += 1
                k = j
                while k < n and s[k] == " ":
                    k += 1
                if k < n and s[k] == "(":
                    end = _balanced_end(s, k)
                    out.append((s[i:j], norm(s[i:end])))
                    scan(s[k + 1:end - 1])
                    i = end - 1
                else:
                    i = j - 1
            i += 1

    scan(formula)
    return out


def formula_cost(text):
    """Rank an expression by the costly function families inside it. Plain arithmetic and If() cost nothing here."""
    return sum(w * len(rx.findall(text)) for _, rx, w in FORMULA_FAMILY)


def formula_family(text):
    best = None
    for fam, rx, w in FORMULA_FAMILY:
        if rx.search(text) and (best is None or w > best[1]):
            best = (fam, w)
    return best[0] if best else None


def lookup_key_args(formula):
    """For each Lookup(...) in a formula, the argument expressions after the first (the key columns)."""
    out = []
    for name, text in split_calls(formula):
        if name.lower() != "lookup":
            continue
        inner = text[text.index("(") + 1:-1]
        args, depth, cur, i, q = [], 0, "", 0, None
        while i < len(inner):
            c = inner[i]
            if q:
                cur += c
                if c == q:
                    q = None
            elif c in "\"'":
                q = c
                cur += c
            elif c in "([":
                depth += 1
                cur += c
            elif c in ")]":
                depth -= 1
                cur += c
            elif c == "," and depth == 0:
                args.append(cur.strip())
                cur = ""
            else:
                cur += c
            i += 1
        args.append(cur.strip())
        out.append(args[1:])
    return out


# ---------------------------------------------------------------------------------------------------------------
# The detectors. `ctx` is supplied by recommend.py and gives them the graph, the helpers, and add().
# ---------------------------------------------------------------------------------------------------------------
def tableset(p, minimum=1):
    return {t for t in p["tables"]}


def add_perf_recs(d, ctx):
    comp = d.get("compiled") or {}
    profiles = {eid: c["profile"] for eid, c in comp.items() if c.get("profile")}
    formulas = d.get("formulas") or {}
    g = ctx.g
    wb_oid = d["workbook"]["id"]
    ek = lambda eid: f"{wb_oid}|{eid}"
    base_name = lambda knode: norm(ctx.ename(knode)[0].replace("\xa0", " "))        # untitled elements get a readable name
    pairs = [(e, base_name(ek(e))) for e in comp]
    pairs += [(k.split("|")[1], base_name(k)) for k in {ctx.ek_of(x) for x in ctx.col_nodes} if k.split("|")[1] not in comp]
    labels = unique_labels(pairs)
    name_of = lambda eid: labels.get(eid) or eid

    def nodes_of(eid, pred=None, limit=4):
        """Graph nodes of an element's columns (calculated ones first), optionally filtered by a formula predicate."""
        keys = [k for k in ctx.by_elem.get(ek(eid), []) if (pred is None or pred(formulas.get(k) or g[k]["f"] or ""))]
        keys.sort(key=lambda k: g[k]["raw"])
        return keys[:limit]

    # ---- A. elements that re-run the same stack of joins ------------------------------------------------------
    heavy = {eid: p for eid, p in profiles.items() if len(p["tables"]) >= 3 and p["joins"] >= 4}
    parent = {e: e for e in heavy}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    ids = sorted(heavy)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            sa, sb = tableset(heavy[a]), tableset(heavy[b])
            if len(sa & sb) / len(sa | sb) >= 0.75:
                parent[find(a)] = find(b)
    groups = collections.defaultdict(list)
    for e in heavy:
        groups[find(e)].append(e)
    for members in groups.values():
        if len(members) < 2:
            continue
        joins = sorted((heavy[m]["joins"] for m in members), reverse=True)
        saved = sum(joins) - joins[0]
        shared = set.intersection(*[tableset(heavy[m]) for m in members])
        names = [name_of(m) for m in members]
        # the nearest element they all draw from, where the shared joins could be done once (and materialized)
        anc = [ctx.ancestors(ek(m)) for m in members]
        common = set(anc[0]).intersection(*[set(a) for a in anc[1:]])
        home = min(common, key=lambda r: sum(a.get(r, 99) for a in anc)) if common else None
        home_name = ctx.ename(home)[0] if home else None
        verdict = "Likely faster" if (len(members) >= 3 or joins[0] >= 10) else "May be faster"
        mat = sorted(short_table(t) for t in shared if re.search(r"\.\"?T_MAT_", t.upper()))
        nodes, roles = [], {}
        for m in members:
            for k in nodes_of(m, limit=2):
                nodes.append(k)
                roles[k] = "Its query re-runs the shared joins"
        ctx.add("perf-shared-joins", "high" if verdict == "Likely faster" else "med",
                f"{len(members)} elements each run the same {len(shared)}-table join stack",
                f"The SQL for {and_list(names)} joins the same {len(shared)} tables "
                f"({and_list(sorted(short_table(t) for t in shared), 5)}), using {and_list([str(j) for j in joins], 5)} joins. "
                f"Each element compiles to its own query that includes everything upstream, so the stack runs once per element, "
                f"about {sum(joins)} joins in all."
                + (f" The stack already reads {mat[0]}, so the joins to remove are the ones beyond it." if mat else ""),
                (f"Do the joins once in the '{home_name}' element, which they all draw from, and have the others read its output. "
                 f"Materializing that element, or moving the joins into a data model or the warehouse, lets the others skip the work."
                 if home_name else
                 "Do the joins once in a shared element, data model or warehouse table, and have these elements read from it."),
                nodes, f"about {saved} fewer joins across {len(members)} queries", names,
                roles=roles, verdict=verdict, confidence="Measured", impact=saved * W_JOIN,
                evidence=f"{sum(joins)} joins across {len(members)} generated queries over {len(shared)} shared tables",
                verify="Compare the query history for these elements before and after, or count the joins in each element's SQL.")

    # ---- B. one query that reads the same table several times -------------------------------------------------
    for eid, p in profiles.items():
        again = {t: n for t, n in p["tables"].items() if n >= 3}
        if not again:
            continue
        t, n = max(again.items(), key=lambda x: x[1])
        lookups = nodes_of(eid, lambda f: "Lookup(" in f, limit=4)
        ctx.add("perf-repeated-scan", "med",
                f"{name_of(eid)} reads {short_table(t)} {n} times in one query",
                f"The SQL for {name_of(eid)} reads {short_table(t)} {n} times" + (f", and {plural(len(again) - 1, 'other table')} repeatedly" if len(again) > 1 else "")
                + ". Each extra read is another scan of that table inside the same query.",
                "Reduce the number of separate lookups or joins into this table: join it once and reference its columns, or look up one "
                "row of needed columns together. If it is a large table, this is the first place to look.",
                lookups, f"up to {n - 1} fewer reads", [name_of(eid)],
                roles={k: "Possible source of an extra read" for k in lookups}, verdict="May be faster", confidence="Measured",
                impact=(n - 1) * W_EXTRA_SCAN,
                evidence=f"{short_table(t)} appears {n} times in the generated SQL",
                verify="Open the element's query profile in the warehouse and look for repeated scans of that table.")

    # ---- C. lookups the compiler could not prove return one row ------------------------------------------------
    for eid, p in profiles.items():
        if p["guarded"] >= 5:
            lookups = nodes_of(eid, lambda f: "Lookup(" in f, limit=5)
            ctx.add("perf-uncertain-lookups", "med",
                    f"{name_of(eid)}: {p['guarded']} values are guarded against more than one match",
                    f"The SQL for {name_of(eid)} wraps {p['guarded']} values in min/max with a null-safe comparison. That is how Sigma "
                    f"shows a value it cannot prove is a single value per key, whether it comes from a lookup or from a grouped table, "
                    f"and it adds work to every such column.",
                    "If they come from lookups, make the looked-up side unique on its key (group it by the key, or use a table whose key "
                    "is declared unique) so the lookup compiles to a plain join. If they come from a grouped table, group only by the "
                    "keys you need. This is a hypothesis: confirm the key really is unique first.",
                    lookups, f"up to {p['guarded']} guarded values", [name_of(eid)],
                    roles={k: "A lookup that may not be provably one row" for k in lookups}, verdict="May be faster", confidence="Measured",
                    impact=p["guarded"] * W_GUARD,
                    evidence=f"{p['guarded']} min/max null-equality guards in the generated SQL",
                    verify="Check that the key is unique in the source, then compare the element's SQL before and after.")

    # ---- D. key expressions wrapped in a function ---------------------------------------------------------------
    wrapped_by_elem = collections.defaultdict(list)
    for k, n in ctx.col_nodes.items():
        f = formulas.get(k) or n["f"] or ""
        if "Lookup(" not in f:
            continue
        for keys in lookup_key_args(f):
            if any("(" in a and not re.fullmatch(r"\[[^\[\]]+\]", a) for a in keys):
                wrapped_by_elem[ctx.ek_of(k)].append(k)
                break
    for eknode, ks in wrapped_by_elem.items():
        eid = eknode.split("|")[1]
        p = profiles.get(eid)
        measured = bool(p and p["cast_in_join"])
        ctx.add("perf-wrapped-key", "med" if measured else "low",
                f"{name_of(eid)}: {plural(len(ks), 'lookup')} {'matches' if len(ks) == 1 else 'match'} on a key wrapped in a function",
                f"{plural(len(ks), 'Lookup column')} in {name_of(eid)} {'matches' if len(ks) == 1 else 'match'} on a key that has a function or "
                f"cast around it" + (f", and the generated SQL has {plural(p['cast_in_join'], 'join condition')} with a cast." if measured else ".")
                + " A transformed key hides the plain column from the optimizer, which can stop it from simplifying the join.",
                "Compute the cleaned-up key once in a helper column on each side (or fix the type in the source) and look up on the plain column.",
                ks[:5], f"{len(ks)} join{'s' if len(ks) > 1 else ''} to simplify", [name_of(eid)],
                roles={k: "Matches on a transformed key" for k in ks[:5]}, verdict="May be faster", impact=len(ks) * W_JOIN,
                confidence="Measured" if measured else "Inferred",
                evidence=(f"{plural(p['cast_in_join'], 'join condition')} with a cast in the generated SQL" if measured else "key expressions with functions around them"),
                verify="Compare the join conditions in the element's SQL after switching to plain key columns.")

    # ---- E. expensive logic repeated across columns of one element (a helper column would hold it once) --------
    by_elem_cols = collections.defaultdict(list)
    for k, n in ctx.col_nodes.items():
        f = formulas.get(k) or n["f"] or ""
        if not n["raw"] and f and n["ok"] == "workbook":
            by_elem_cols[ctx.ek_of(k)].append((k, norm(f)))
    for eknode, cols in by_elem_cols.items():
        if len(cols) < 2:
            continue
        eid = eknode.split("|")[1]
        occ = collections.defaultdict(set)                     # sub-expression text -> columns containing it
        for k, f in cols:
            seen = {f}
            seen.update(t for _, t in split_calls(f))
            for t in seen:
                if formula_cost(t) >= 2:
                    occ[t].add(k)
        colf = dict(cols)                                        # column node -> normalized formula
        cands = {t: ks for t, ks in occ.items() if len(ks) >= 2}
        # drop a candidate when a larger one is held by the same columns, and groups that are whole-formula duplicates only
        keep = {}
        for t, ks in cands.items():
            if any(u != t and t in u and cands[u] == ks for u in cands):
                continue
            if all(colf[k] == t for k in ks):
                continue                                       # identical whole formulas are the 'same formula' finding
            keep[t] = ks
        p = profiles.get(eid)
        for t, ks in sorted(keep.items(), key=lambda x: -formula_cost(x[0]) * len(x[1]))[:6]:
            fam = formula_family(t)
            sqlfam = SQL_FOR_FAMILY.get(fam)
            confirmed = bool(p and sqlfam and any(r["family"] == sqlfam and r["count"] >= 2 for r in p["repeats"]))
            holder = next((k for k in ks if colf[k] == t), None)
            roles = {k: ("Already holds this logic: call it" if k == holder else
                         "Repeats it inside a larger formula" if holder else "Repeats this logic") for k in ks}
            cost = formula_cost(t)
            verdict = "Likely faster" if (confirmed or cost >= 50) else "May be faster"
            ctx.add("perf-shared-subexpression", "high" if verdict == "Likely faster" else "med",
                    f"{name_of(eid)}: {len(ks)} columns repeat the same {fam or 'costly'} logic",
                    f"In {name_of(eid)}, {len(ks)} columns contain {t[:120]}{'…' if len(t) > 120 else ''}."
                    + (f" The generated SQL repeats the matching {sqlfam} call {max(r['count'] for r in p['repeats'] if r['family'] == sqlfam)} times." if confirmed else
                       " The generated SQL may already share it, so check before spending time."),
                    ("Make the other columns reference the column that already holds it." if holder else
                     "Add a helper column holding this logic once, and reference it from these columns."),
                    sorted(ks), f"{len(ks)} evaluations to 1", [name_of(eid)],
                    roles=roles, verdict=verdict, confidence="Measured" if confirmed else "Inferred",
                    impact=(len(ks) - 1) * max(cost, 1),
                    evidence=("repeated in the generated SQL" if confirmed else f"same {fam or 'costly'} expression in {len(ks)} formulas"),
                    verify="Look for the repeated call in the element's SQL; if it appears once, the compiler already shares it.")


# ---------------------------------------------------------------------------------------------------------------
# A verdict for the recommendations that were not written with performance in mind
# ---------------------------------------------------------------------------------------------------------------
def annotate(d, recs, ctx):
    comp = d.get("compiled") or {}
    profiles = {eid: c["profile"] for eid, c in comp.items() if c.get("profile")}
    wb_oid = d["workbook"]["id"]
    have_sql = bool(profiles)
    g = ctx.g
    for r in recs:
        if r.get("verdict"):
            continue
        t = r["type"]
        eids = {k.split("|")[2] for k in r["nodes"] if k.split("|")[0] == "c" and k.split("|")[1] == wb_oid and k in g}
        joins = sum(profiles[e]["joins"] for e in eids if e in profiles)
        measured = have_sql and bool(eids & set(profiles))
        conf = "Measured" if measured else "Inferred"
        if t == "dead-column":
            r.update(verdict="Mostly maintainability", confidence="Inferred", impact=0,
                     evidence="unused columns are left out of the generated SQL",
                     verify="Not needed: removing them tidies the workbook but does not change the query.")
        elif t == "dup-formula":
            elements = {ctx.ek_of(k) for k in r["nodes"] if k in ctx.col_nodes}
            f = next((g[k]["f"] for k in r["nodes"] if k in g), "") or ""
            cost = formula_cost(f)
            if len(elements) < 2:
                r.update(verdict="Mostly maintainability", confidence="Inferred", impact=0,
                         evidence="identical logic inside one element is generated once",
                         verify="Check the element's SQL: the logic should appear once.")
            elif cost >= 3:
                r.update(verdict="May be faster", confidence="Inferred", impact=(len(r["nodes"]) - 1) * cost,
                         evidence=f"costly logic recomputed by {len(elements)} separate queries",
                         verify="Each element's query repeats it. Moving it upstream helps most if that element is materialized.")
            else:
                r.update(verdict="Mostly maintainability", confidence="Inferred", impact=0,
                         evidence="cheap logic; each element's query repeats it",
                         verify="Cheap expressions rarely change run time. The gain is one place to edit.")
        elif t == "dup-element":
            ps = [profiles[e] for e in eids if e in profiles]
            heavy = sum(cost_index(p) for p in ps)
            costs = sorted((cost_index(p) for p in ps), reverse=True)
            r.update(verdict="Likely faster" if heavy >= 30 else "May be faster", impact=sum(costs[1:]),
                     confidence="Measured" if ps else "Inferred",
                     evidence=(f"these elements' generated queries total a cost index of {heavy:g} (a ranking weight, not seconds)" if ps
                               else "separate elements compile to separate queries"),
                     verify="Each element is its own query, so keeping one saves the other's query. Confirm with query history.")
        elif t == "multi-lookup":
            r.update(verdict="Likely faster" if (measured and joins >= len(r["nodes"])) else "May be faster", confidence=conf,
                     impact=max(len(r["nodes"]) - 1, 1) * W_JOIN,
                     evidence=(f"the element's generated SQL has {joins} joins" if measured else "each Lookup compiles to a join"),
                     verify="Count the joins in the element's SQL before and after.")
        elif t == "lookup":
            r.update(verdict="May be faster", confidence=conf, impact=W_JOIN,
                     evidence=("removes a join from the element's generated SQL" if measured else "a Lookup compiles to a join"),
                     verify="Confirm the two columns hold the same value, then compare the element's SQL.")
        else:
            r.update(verdict="May be faster", confidence="Inferred", impact=1, evidence="", verify="")


def summarize(d, recs):
    """The element table and headline numbers for the Performance outlook section."""
    comp = d.get("compiled") or {}
    rows = []
    for eid, c in comp.items():
        p = c.get("profile")
        if not p:
            continue
        rows.append({"id": eid, "name": c.get("name") or eid, "cost": cost_index(p), "joins": p["joins"], "tables": len(p["tables"]),
                     "rescans": sum(n - 1 for n in p["tables"].values() if n > 1), "windows": p["windows"], "regex": p["regex"],
                     "json": p["json"] + p["flatten"], "ai": p["ai"], "guarded": p["guarded"], "distinct_aggs": p["distinct_aggs"],
                     "bytes": p["bytes"]})
    rows.sort(key=lambda r: -r["cost"])
    perf = [r for r in recs if r.get("verdict")]
    return {"available": bool(rows), "elements": rows[:25], "n_elements": len(rows),
            "totals": {"joins": sum(r["joins"] for r in rows), "rescans": sum(r["rescans"] for r in rows),
                       "windows": sum(r["windows"] for r in rows), "guarded": sum(r["guarded"] for r in rows)},
            "verdicts": dict(collections.Counter(r["verdict"] for r in perf)),
            "top": [r["id"] for r in sorted(perf, key=lambda r: (VERDICTS.index(r["verdict"]), -(r.get("impact") or 0),
                                                                   0 if r.get("confidence") == "Measured" else 1))
                    if r["verdict"] != "Mostly maintainability"][:5]}
