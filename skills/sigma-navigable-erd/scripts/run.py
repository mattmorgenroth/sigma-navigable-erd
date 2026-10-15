#!/usr/bin/env python3
"""Build a column-level lineage + ownership page ("blame ERD") for a Sigma workbook.

  python3 run.py check [--env-file FILE]
  python3 run.py trace <workbook URL | urlId | workbook UUID> [--env-file FILE] [--out DIR]
                       [--owners owners.json] [--title TITLE]

Credentials come from environment variables or an env file (never command-line flags):
  SIGMA_BASE_URL, SIGMA_CLIENT_ID, SIGMA_CLIENT_SECRET
The env file defaults to $SIGMA_ENV_FILE, then ~/.sigma-lineage/credentials.env, and must be
readable only by you (chmod 600). Everything is read-only (GET requests).
"""
import argparse, datetime, json, os, re, shutil, stat, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blame  # noqa: E402
import recommend  # noqa: E402
from render import render_html  # noqa: E402

DEFAULT_ENV = os.path.expanduser("~/.sigma-lineage/credentials.env")
DEFAULT_OUT = os.path.expanduser("~/sigma-lineage")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
NEEDED = ("SIGMA_BASE_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET")


def load_env(path):
    if not os.path.exists(path):
        return False
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"{path} is readable by other users. Run: chmod 600 {path}")
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


def setup(env_file):
    path = env_file or os.environ.get("SIGMA_ENV_FILE") or DEFAULT_ENV
    found = load_env(path)
    missing = [k for k in NEEDED if not os.environ.get(k)]
    if missing:
        where = path if found else f"{path} (not found)"
        raise SystemExit("Missing " + ", ".join(missing) + f". Set them in {where} or the environment. "
                         "See the skill's setup section. Never paste them into a chat.")
    blame.configure(os.environ["SIGMA_BASE_URL"])


def resolve_workbook(target):
    """Accept a workbook UUID, a url-id, or a full workbook URL; return the workbook UUID."""
    if UUID.match(target):
        return target
    path = urllib.parse.urlparse(target).path if "://" in target else target
    seg = [p for p in path.split("/") if p][-1] if path else target
    seg = seg.split("?")[0]
    urlid = seg.split("-")[-1] if "-" in seg else seg
    page = None
    while True:
        qs = {"limit": "1000", "typeFilters": "workbook"}
        if page:
            qs["page"] = page
        d = json.loads(blame.api("/v2/files?" + urllib.parse.urlencode(qs)))
        for e in d.get("entries", []):
            if e.get("urlId") in (urlid, seg):
                return e["id"]
        page = d.get("nextPage")
        if not page:
            break
    raise SystemExit(f"Could not find a workbook with url-id {urlid!r} that these credentials can see. "
                     "Check the link and that the API user has access to the workbook.")


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:60] or "workbook"


STANDALONE_HEAD = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                   '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>\n')


def standalone_html(page):
    """The same page wrapped as a complete document, for opening from disk or sending as a file."""
    return STANDALONE_HEAD + page + "\n</body></html>\n"


def recommendations_md(d):
    recs = d.get("recommendations", [])
    lines = [f"# Duplicate-logic recommendations: {d['workbook']['name']}", "",
             f"{len(recs)} findings. Each is a heuristic; verify before removing.", ""]
    for r in recs:
        lines += [f"## [{r['sev'].upper()}] {r['title']}", f"- Type: {r['type']}  |  Saves: {r['saves']}",
                  f"- Where: {'; '.join(r['where'])}", f"- Owners to involve: {', '.join(r['owners']) or 'n/a'}",
                  f"- Why: {r['detail']}", f"- Do: {r['action']}", ""]
    return "\n".join(lines)


def cmd_check(args):
    setup(args.env_file)
    who = json.loads(blame.api("/v2/whoami"))
    print("Authenticated OK.")
    print("  API host:        ", blame.BASE)
    print("  Organization ID: ", who.get("organizationId"))
    print("  API user ID:     ", who.get("userId"))
    print("The workbook and its data models must be visible to this API user for a full trace.")


def cmd_trace(args):
    setup(args.env_file)
    overrides = {}
    if args.owners:
        overrides = {k: v for k, v in json.load(open(args.owners)).items() if not k.startswith("_")}
    wid = resolve_workbook(args.target)
    print("Tracing (this can take a minute or two for large workbooks)...")
    d = blame.trace_workbook(wid, overrides)
    d["recommendations"] = recommend.recommend(d)
    wb = d["workbook"]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    host = urllib.parse.urlparse(blame.BASE).hostname
    root = os.path.join(args.out, host, f"{slug(wb['name'])}-{wid[:8]}")
    run_dir = os.path.join(root, stamp)
    os.makedirs(run_dir, exist_ok=True)
    json.dump(d, open(os.path.join(run_dir, "blame.json"), "w"))
    page = render_html(d, args.title)
    open(os.path.join(run_dir, "recommendations.md"), "w").write(recommendations_md(d))
    open(os.path.join(run_dir, "standalone.html"), "w").write(standalone_html(page))
    open(os.path.join(run_dir, "index.html"), "w").write(page)
    os.makedirs(os.path.join(root, "latest"), exist_ok=True)
    shutil.copy(os.path.join(run_dir, "index.html"), os.path.join(root, "latest", "index.html"))
    shutil.copy(os.path.join(run_dir, "standalone.html"), os.path.join(root, "latest", "standalone.html"))
    cols = [c for e in d["elements"] for c in e["columns"]]
    summary = (f"{len(d['elements'])} elements, {len(cols):,} columns "
               f"({sum(1 for c in cols if not c['raw']):,} calculated), {len(d['objects'])} Sigma objects")
    with open(os.path.join(args.out, "INDEX.md"), "a") as f:
        f.write(f"- {stamp} | {wb['name']} | {host} | {summary} | {os.path.join(run_dir, 'index.html')}\n")
    s = recommend.summarize(d["recommendations"])
    summary += f"; {s['total']} duplicate-logic recommendations ({s['by_sev'].get('high', 0)} high)"
    print(summary)
    print("Saved:", os.path.join(run_dir, "index.html"))
    print("Latest copy:", os.path.join(root, "latest", "index.html"))
    print("Run log:", os.path.join(args.out, "INDEX.md"))
    partial = sum(1 for c in cols if c.get("partial"))
    if partial:
        print(f"Note: {partial} columns end at a data model whose upstream element the API does not expose.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="verify credentials and host without tracing anything")
    c.add_argument("--env-file")
    t = sub.add_parser("trace", help="trace a workbook and save the ERD page")
    t.add_argument("target", help="workbook URL, url-id, or UUID")
    t.add_argument("--env-file")
    t.add_argument("--out", default=DEFAULT_OUT, help=f"central output folder (default {DEFAULT_OUT})")
    t.add_argument("--owners", help="JSON file mapping warehouse table prefixes to owners")
    t.add_argument("--title", help="page title (default: 'Navigable Entity Relationship Diagram')")
    a = ap.parse_args()
    {"check": cmd_check, "trace": cmd_trace}[a.cmd](a)


if __name__ == "__main__":
    main()
