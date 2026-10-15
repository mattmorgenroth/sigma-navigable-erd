"""End-to-end tests against the fictional Acme Retail fixture. No network, no credentials.

Run from the repository root:   python3 -m unittest discover -s tests -v
"""
import io
import os
import re
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "skills", "sigma-navigable-erd", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "examples"))

import blame  # noqa: E402
import fake_sigma  # noqa: E402
import recommend  # noqa: E402
import run  # noqa: E402
from render import render_html  # noqa: E402


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_sigma.install(blame)
        cls.d = blame.trace_workbook(fake_sigma.WB_ID, {})
        cls.d["recommendations"] = recommend.recommend(cls.d)
        cls.g = cls.d["graph"]
        cls.recs = cls.d["recommendations"]

    def of_type(self, t):
        return [r for r in self.recs if r["type"] == t]

    # ---- lineage and ownership -------------------------------------------------------------------------
    def test_objects_and_owners_are_resolved(self):
        owners = {o["name"]: o["owner"] for o in self.d["objects"]}
        self.assertEqual(owners["Sales Performance Overview"], "Alex Rivera")
        self.assertEqual(owners["Revenue Model"], "Priya Shah")
        self.assertEqual(owners["Customer Dimension"], "Sam Okafor")

    def test_every_column_is_traced(self):
        cols = [c for e in self.d["elements"] for c in e["columns"]]
        self.assertEqual(len(self.d["elements"]), 8)
        self.assertEqual(len(cols), 57)
        self.assertFalse([c["name"] for c in cols if c["unresolved"]])

    def test_unnamed_columns_take_the_names_the_api_reports(self):
        by = {e["name"]: [c["name"] for c in e["columns"]] for e in self.d["elements"]}
        self.assertIn("Row Count", by["Revenue by Segment"])
        self.assertIn("Values of Tags", by["Customer Tags"])
        self.assertFalse([n for ns in by.values() for n in ns if n in ("rs04", "un03")])

    def test_unnest_and_code_outputs_are_traced(self):
        by = {e["name"]: {c["name"]: c for c in e["columns"]} for e in self.d["elements"]}
        tags = by["Customer Tags"]
        self.assertFalse(tags["Values of Tags"]["raw"])                         # derived from the array column
        self.assertFalse(tags["Values of Tags"]["unresolved"])
        self.assertTrue(tags["Customer Id"]["raw"])                             # passes straight through the unnest
        self.assertFalse(by["Alert Message"]["Message"]["unresolved"])          # a code output is a terminal, not a gap

    def test_the_row_count_reference_resolves_to_its_unnamed_column(self):
        by = {e["name"]: {c["name"]: c for c in e["columns"]} for e in self.d["elements"]}
        self.assertFalse(by["Revenue by Segment"]["Rows Doubled"]["unresolved"])

    def test_pass_through_vs_calculated(self):
        by = {e["name"]: {c["name"]: c for c in e["columns"]} for e in self.d["elements"]}
        self.assertTrue(by["Customers"]["Tier"]["raw"])                       # plain reference all the way down
        self.assertFalse(by["Customers"]["Segment"]["raw"])                   # the data model applies an If()
        self.assertEqual(by["Customers"]["Segment"]["owners"], ["Sam Okafor"])  # ...and Sam owns that logic
        self.assertFalse(by["Revenue Summary"]["Net Revenue"]["raw"])
        self.assertEqual(by["Revenue Summary"]["Net Revenue"]["owners"], ["Priya Shah"])

    def test_column_graph_reaches_the_warehouse(self):
        node = next(k for k, n in self.g.items() if n["k"] == "col" and n["c"] == "Net Revenue" and n["e"] == "Revenue Summary")
        seen, stack, terminals = set(), [node], set()
        while stack:
            k = stack.pop()
            if k in seen:
                continue
            seen.add(k)
            if self.g[k]["k"] == "wh":
                terminals.add((self.g[k]["o"], self.g[k]["e"], self.g[k]["c"]))
            stack += self.g[k]["ups"]
        self.assertIn(("ANALYTICS.MARTS", "ORDERS", "Amount"), terminals)
        self.assertIn(("ANALYTICS.MARTS", "ORDERS", "Discount"), terminals)

    def test_links_open_in_edit_mode(self):
        urls = [n["u"] for n in self.g.values() if n["u"]]
        self.assertTrue(urls)
        for u in urls:
            self.assertRegex(u.split("?")[0], r"/edit$")
        self.assertTrue(any(u.endswith("/edit?:nodeId=eOrd") for u in urls))

    def test_unmapped_warehouse_tables_say_owner_not_set(self):
        teams = {n["w"] for n in self.g.values() if n["k"] == "wh"}
        self.assertEqual(teams, {blame.NOOWNER})

    def test_owners_file_maps_warehouse_prefixes(self):
        d = blame.trace_workbook(fake_sigma.WB_ID, {"ANALYTICS.MARTS": "Data Eng", "ANALYTICS.MARTS.CUSTOMERS": "CRM Team"})
        teams = {n["w"] for n in d["graph"].values() if n["k"] == "wh"}
        self.assertEqual(teams, {"Data Eng", "CRM Team"})    # longest prefix wins

    # ---- recommendations -------------------------------------------------------------------------------
    def test_redundant_lookup_is_found(self):
        r = [r for r in self.of_type("lookup") if "Customer Segment" in r["title"]]
        self.assertEqual(len(r), 1)
        self.assertIn("SEGMENT", r[0]["detail"])        # the column the source table already has
        self.assertEqual(r[0]["owners"], ["Alex Rivera"])

    def test_redundant_lookup_isolate_view_includes_the_column_to_use_instead(self):
        r = [r for r in self.of_type("lookup") if "Customer Segment" in r["title"]][0]
        roles = set(r["roles"].values())
        self.assertEqual(roles, {"Remove this lookup", "Use this column instead"})
        use = next(k for k, v in r["roles"].items() if v == "Use this column instead")
        self.assertEqual((self.g[use]["e"], self.g[use]["c"]), ("ORDERS", "SEGMENT"))
        self.assertNotIn(blame.NOOWNER, r["owners"])          # a placeholder is never someone to invite

    def test_repeated_lookups_become_one_join(self):
        r = self.of_type("multi-lookup")
        self.assertEqual(len(r), 1)
        self.assertIn("4 lookups", r[0]["title"])

    def test_same_formula_in_several_places(self):
        r = [r for r in self.of_type("dup-formula") if "Order Size" in r["title"]]
        self.assertEqual(len(r), 1)
        self.assertEqual(len(r[0]["nodes"]), 3)

    def test_elements_repeating_calculated_columns_are_grouped(self):
        r = self.of_type("dup-element")
        self.assertEqual(len(r), 1)
        self.assertIn("Orders Export", r[0]["title"])

    def test_unused_hidden_column_is_found_and_grammar_is_right(self):
        r = self.of_type("dead-column")
        self.assertEqual(len(r), 1)
        self.assertIn("Scratch", r[0]["detail"])
        self.assertIn("is not used", r[0]["title"])

    def test_clean_elements_are_not_flagged(self):
        flagged = " ".join(r["title"] + r["detail"] for r in self.recs)
        self.assertNotIn("Customer Dimension", flagged)
        # Revenue Summary does share a join stack with Revenue by Segment (a performance finding), but it repeats no logic
        duplication = " ".join(r["title"] + r["detail"] for r in self.recs if not r["type"].startswith("perf-"))
        self.assertNotIn("Revenue Summary", duplication)

    def test_pass_through_only_overlap_is_not_a_duplicate(self):
        # Orders by Customer and the exports all read the ORDERS table, so they share plain column names.
        for r in self.of_type("dup-element"):
            self.assertNotIn("Orders by Customer", r["title"])

    def test_every_recommendation_has_involved_cards_with_roles(self):
        for r in self.recs:
            self.assertTrue(r["nodes"], r["title"])
            self.assertTrue(r["roles"], r["title"])
            for k in r["nodes"]:
                self.assertIn(k, self.g)

    # ---- rendering -------------------------------------------------------------------------------------
    def test_page_renders_and_is_self_contained(self):
        page = render_html(self.d)
        self.assertIn("<title>Navigable Entity Relationship Diagram</title>", page)
        self.assertIn("Sales Performance Overview", page)
        self.assertNotRegex(page, r">\s*(undefined|NaN|null)\s*<")      # no unfilled values rendered as text
        self.assertNotRegex(page, r"https?://(?!fonts\.googleapis\.com|fonts\.gstatic\.com|app\.sigmacomputing\.com)")

    def test_standalone_page_declares_charset(self):
        s = run.standalone_html(render_html(self.d))
        self.assertTrue(s.startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', s)


class CliTests(unittest.TestCase):
    def setUp(self):
        fake_sigma.install(blame)

    def test_trace_from_a_pasted_url_saves_organized_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = sys.argv
            sys.argv = ["run.py", "trace", fake_sigma.WB_URL, "--out", tmp, "--env-file", os.path.join(tmp, "none.env")]
            try:
                with redirect_stdout(io.StringIO()) as out:
                    run.main()
            finally:
                sys.argv = argv
            self.assertIn("recommendations", out.getvalue())
            self.assertTrue(os.path.exists(os.path.join(tmp, "INDEX.md")))
            found = {os.path.basename(p) for root, _, files in os.walk(tmp) for p in files}
            self.assertTrue({"index.html", "standalone.html", "blame.json", "recommendations.md"} <= found)

    def test_every_form_of_workbook_link_resolves(self):
        base = fake_sigma.WB_URL
        for link in (base, base + "/", base + "/edit", base + "/edit?:nodeId=eOrd", base + "?:nodeId=eOrd",
                     base + "/tag/Production", base + "/tag/Production?:nodeId=eOrd&:customView=abc",
                     base.replace("https://", "http://"), base.split("/workbook/")[1]):
            self.assertEqual(run.resolve_workbook(link), fake_sigma.WB_ID, link)
        with self.assertRaises(SystemExit):
            run.resolve_workbook(fake_sigma.BASE_URL + "/workbook/Not-A-Real-Workbook-zzzzzzzz")

    def test_url_id_and_uuid_resolve_to_the_same_workbook(self):
        self.assertEqual(run.resolve_workbook(fake_sigma.WB_URL), fake_sigma.WB_ID)
        self.assertEqual(run.resolve_workbook(fake_sigma.WB_URLID), fake_sigma.WB_ID)
        self.assertEqual(run.resolve_workbook(fake_sigma.WB_ID), fake_sigma.WB_ID)

    def test_refuses_non_sigma_hosts(self):
        for bad in ("https://evil.example.com", "http://api.sigmacomputing.com", "https://sigmacomputing.com.evil.io"):
            with self.assertRaises(SystemExit):
                blame.configure(bad)
        blame.configure("https://aws-api.sigmacomputing.com")     # a real one is accepted
        blame.configure("https://api.sigmacomputing.com")

    def test_refuses_an_env_file_with_a_secret_that_other_users_can_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "creds.env")
            with open(p, "w") as f:
                f.write('SIGMA_BASE_URL="https://api.sigmacomputing.com"\n')
            os.chmod(p, 0o644)
            self.assertTrue(run.load_env(p))              # only the API host: nothing sensitive, so 644 is fine
            with open(p, "w") as f:
                f.write('SIGMA_BASE_URL="https://api.sigmacomputing.com"\nSIGMA_CLIENT_SECRET="not-a-real-secret"\n')
            os.chmod(p, 0o644)
            with self.assertRaises(SystemExit) as cm:
                run.load_env(p)
            self.assertIn("chmod 600", str(cm.exception))
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            self.assertTrue(run.load_env(p))


class HygieneTests(unittest.TestCase):
    """The repository must never contain credentials, real contact details or machine-specific paths."""

    BAD = re.compile(
        r"(client_secret|api[_-]?key|token)\s*[=:]\s*[\"']?[0-9a-fA-F]{32,}"      # a credential-shaped value
        r"|[\w.+-]+@(?!example\.(com|org)\b)[\w-]+\.[a-z]{2,}"                  # any email that is not an example address
        r"|/Users/[\w.-]+/|/home/[\w.-]+/|C:\\Users\\"                          # a local machine path
        r"|(slack\.com/archives|atlassian\.net|docs\.google\.com/document)"       # internal chat, ticket or document links
        r"|" + "|".join(("Rel" "IR", "hyper" "sheets?", "c4" "_\\w+", "mono" "-rust", "rr" "-debug", "ATT" "R-ization")),   # a vendor's internal vocabulary
        re.I)

    def test_no_secrets_contact_details_or_local_paths_in_any_file(self):
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                if name == "test_offline.py":
                    continue          # this file contains the patterns themselves
                path = os.path.join(base, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except (UnicodeDecodeError, OSError):
                    continue
                m = self.BAD.search(text)
                self.assertIsNone(m, f"unexpected value in {os.path.relpath(path, ROOT)}: {m.group(0)[:40] if m else ''}")


if __name__ == "__main__":
    unittest.main()
