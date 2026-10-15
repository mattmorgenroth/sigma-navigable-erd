#!/usr/bin/env python3
"""Build a column-level lineage + ownership page (Navigable Entity Relationship Diagram) for a Sigma workbook.

  python3 run.py login  [--base-url URL] [--env-file FILE]        sign in once with your browser
  python3 run.py check  [--auth-mode MODE] [--env-file FILE]      verify the sign-in without tracing anything
  python3 run.py trace <workbook URL | urlId | workbook UUID> [--auth-mode MODE] [--env-file FILE]
                       [--out DIR] [--owners owners.json] [--title TITLE]
  python3 run.py logout [--base-url URL] [--env-file FILE]        forget the saved browser sign-in

Sign-in (--auth-mode or SIGMA_AUTH_MODE): `auto` (default) uses your saved browser sign-in, then API client
credentials, then signs you in through the browser if you are at a terminal. `browser` and `client-credentials`
force one method. A token already in SIGMA_API_TOKEN is used as is.

Settings come from environment variables or an env file (never from command-line flags):
  SIGMA_BASE_URL                           always needed: the API host for your Sigma cloud
  SIGMA_CLIENT_ID, SIGMA_CLIENT_SECRET     only for the client-credentials fallback
The env file defaults to $SIGMA_ENV_FILE, then ~/.sigma-lineage/credentials.env. A file that holds a client secret
must be readable only by you (chmod 600). Everything is read-only.
"""
import argparse, datetime, json, os, re, shutil, stat, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth  # noqa: E402
import blame  # noqa: E402
import recommend  # noqa: E402
from render import render_html  # noqa: E402

DEFAULT_ENV = os.path.expanduser("~/.sigma-lineage/credentials.env")
DEFAULT_OUT = os.path.expanduser("~/sigma-lineage")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def load_env(path):
    """Read KEY=value lines into the environment (existing variables win). Returns False if the file is absent."""
    if not os.path.exists(path):
        return False
    with open(path) as fh:
        pairs = []
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                pairs.append((k.strip(), v.strip().strip('"').strip("'")))
    holds_secret = any(k in ("SIGMA_CLIENT_SECRET", "SIGMA_API_TOKEN") and v for k, v in pairs)
    if holds_secret and os.name != "nt" and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"{path} holds a secret and is readable by other users. Run: chmod 600 {path}")
    for k, v in pairs:
        os.environ.setdefault(k, v)
    return True


def setup(env_file, base_url=None, auth_mode=None):
    path = env_file or os.environ.get("SIGMA_ENV_FILE") or DEFAULT_ENV
    found = load_env(path)
    base = base_url or os.environ.get("SIGMA_BASE_URL")
    if not base:
        where = path if found else f"{path} (not found)"
        raise SystemExit("SIGMA_BASE_URL is not set. Put your Sigma API host in " + where + " or the environment, "
                         "or pass --base-url. It depends on your Sigma cloud, for example https://api.sigmacomputing.com")
    blame.configure(base)
    blame.set_auth_mode(auth_mode)
    return base


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
    setup(args.env_file, args.base_url, args.auth_mode)
    tok = blame.token_info()
    who = json.loads(blame.api("/v2/whoami"))
    print("Authenticated OK, using " + tok.method + ".")
    print("  API host:        ", blame.BASE)
    print("  Organization ID: ", who.get("organizationId"))
    print("  User ID:         ", who.get("userId"))
    print("The workbook and its data models must be visible to this user for a full trace.")


def cmd_login(args):
    base = setup(args.env_file, args.base_url)
    try:
        auth.login(base, interactive=True)
    except auth.AuthError as e:
        raise SystemExit(str(e))
    blame.set_auth_mode("browser")
    who = json.loads(blame.api("/v2/whoami"))
    print("Signed in. The sign-in is saved in your OS keychain for " + base + ".")
    print("  Organization ID: ", who.get("organizationId"))
    print("  User ID:         ", who.get("userId"))


def cmd_logout(args):
    base = setup(args.env_file, args.base_url)
    auth.forget_session(base)
    print("Removed the saved browser sign-in for " + base + ". API client credentials are not affected.")


def cmd_trace(args):
    setup(args.env_file, args.base_url, args.auth_mode)
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

    def common(p, mode=True):
        p.add_argument("--env-file", help="settings file (default: $SIGMA_ENV_FILE, then ~/.sigma-lineage/credentials.env)")
        p.add_argument("--base-url", help="Sigma API host, if not set in SIGMA_BASE_URL")
        if mode:
            p.add_argument("--auth-mode", choices=auth.AUTH_MODES, help="auto (default), browser, or client-credentials")

    common(sub.add_parser("login", help="sign in with your browser and save the session"), mode=False)
    common(sub.add_parser("logout", help="forget the saved browser sign-in"), mode=False)
    common(sub.add_parser("check", help="verify the sign-in and host without tracing anything"))
    t = sub.add_parser("trace", help="trace a workbook and save the ERD page")
    t.add_argument("target", help="workbook URL, url-id, or UUID")
    common(t)
    t.add_argument("--out", default=DEFAULT_OUT, help=f"central output folder (default {DEFAULT_OUT})")
    t.add_argument("--owners", help="JSON file mapping warehouse table prefixes to owners")
    t.add_argument("--title", help="page title (default: 'Navigable Entity Relationship Diagram')")
    a = ap.parse_args()
    {"check": cmd_check, "trace": cmd_trace, "login": cmd_login, "logout": cmd_logout}[a.cmd](a)


if __name__ == "__main__":
    main()
