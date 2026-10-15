"""Performance opinions: reading generated SQL, reading formulas, and giving honest verdicts.

The SQL here is invented (see examples/fake_sigma.py). Run from the repository root:
    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest
import urllib.error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "skills", "sigma-navigable-erd", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "examples"))

import blame  # noqa: E402
import fake_sigma  # noqa: E402
import perf  # noqa: E402
import recommend  # noqa: E402
from render import render_html  # noqa: E402


class ReadingGeneratedSql(unittest.TestCase):
    def test_counts_joins_tables_and_repeat_reads(self):
        sql = ("select 1 from DB.S.A x left join DB.S.B y on x.k = y.k left join (select k from DB.S.B) z on x.k = z.k "
               "left join DB.S.B w on x.k = w.k")
        p = perf.profile_sql(sql)
        self.assertEqual(p["joins"], 3)
        self.assertEqual(p["tables"], {"DB.S.A": 1, "DB.S.B": 3})

    def test_a_cte_name_is_not_a_table_read(self):
        p = perf.profile_sql("with w1 as (select 1 from DB.S.A) select * from w1 join w1 b on 1=1")
        self.assertEqual(p["tables"], {"DB.S.A": 1})

    def test_identical_expensive_calls_are_counted_even_with_nested_parentheses_and_strings(self):
        sql = ("select regexp_substr(lower(trim(A)), '[(]x[)]+') C1, upper(regexp_substr(lower(trim(A)), '[(]x[)]+')) C2, "
               "regexp_substr(B, 'other') C3 from DB.S.T")
        p = perf.profile_sql(sql)
        rep = [r for r in p["repeats"] if r["family"] == "regex"]
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["count"], 2)                     # the two identical calls, not the different one
        self.assertEqual(p["regex"], 3)

    def test_a_parenthesis_inside_a_string_does_not_end_a_call_early(self):
        p = perf.profile_sql("select regexp_substr(A, '(') C1, regexp_substr(A, '(') C2, regexp_substr(B, 'x') C3 from DB.S.T")
        rep = [r for r in p["repeats"] if r["family"] == "regex"]
        self.assertEqual([r["count"] for r in rep], [2])

    def test_whitespace_and_case_do_not_hide_a_repeat(self):
        p = perf.profile_sql("select REGEXP_SUBSTR(A,  'x') C1, regexp_substr(A, 'x') C2 from DB.S.T")
        self.assertEqual(p["repeats"][0]["count"], 2)

    def test_min_max_null_equality_is_counted_as_a_guarded_value(self):
        p = perf.profile_sql("select iff(equal_null(min(X), max(X)), max(X), null) V, iff(equal_null(min(Y), max(Y)), max(Y), null) W from DB.S.T")
        self.assertEqual(p["guarded"], 2)

    def test_a_cast_in_a_join_condition_is_found_but_the_null_safe_pattern_is_not(self):
        cast = perf.profile_sql("select 1 from DB.S.A a left join DB.S.B b on a.K::text = b.K::text where 1=1")
        self.assertEqual(cast["cast_in_join"], 1)
        null_safe = perf.profile_sql("select 1 from DB.S.A a left join DB.S.B b on coalesce(a.K, '') = coalesce(b.K, '') and (a.K is not null) = (b.K is not null)")
        self.assertEqual(null_safe["cast_in_join"], 0)

    def test_windows_distinct_counts_json_and_ai_are_counted(self):
        p = perf.profile_sql("select rank() over (order by A) R, count(distinct B) D, parse_json(C) J, snowflake.cortex.complete('m', D) AI from DB.S.T")
        self.assertEqual((p["windows"], p["distinct_aggs"], p["json"], p["ai"]), (1, 1, 1, 1))

    def test_the_trailing_request_comment_is_ignored(self):
        p = perf.profile_sql('select 1 from DB.S.A\n\n-- Sigma {"sql": "join DB.S.B join DB.S.C"}')
        self.assertEqual((p["joins"], list(p["tables"])), (0, ["DB.S.A"]))

    def test_the_profile_does_not_keep_the_sql(self):
        p = perf.profile_sql("select SECRET_COLUMN from DB.S.A")
        self.assertNotIn("SECRET_COLUMN", json.dumps(p))

    def test_cost_ranks_ai_far_above_a_join(self):
        self.assertGreater(perf.cost_index(perf.profile_sql("select ai_complete(A) from DB.S.T")),
                           perf.cost_index(perf.profile_sql("select 1 from DB.S.A join DB.S.B on 1=1")))


class Readability(unittest.TestCase):
    def test_lists_read_naturally(self):
        self.assertEqual(perf.and_list(["a"]), "a")
        self.assertEqual(perf.and_list(["a", "b", "c"]), "a, b and c")
        self.assertEqual(perf.and_list(list("abcdefg"), 4), "a, b, c, d and 3 more")

    def test_generated_table_names_are_shortened_and_explained(self):
        self.assertEqual(perf.short_table('DB.SCHEMA."SIGDS_CE604F8B_4DCB_4B2B_8BEF_DA86A939EB06"'), "input table SIGDS_CE604F…")
        self.assertEqual(perf.short_table('DB.SCHEMA.T_MAT_53DDA5DFD35E45FB9BC6E4E71DF7D54C_0'), "materialized element T_MAT_53DDA5…")
        self.assertEqual(perf.short_table("ANALYTICS.MARTS.ORDERS"), "ORDERS")

    def test_an_id_is_added_to_a_name_only_when_two_elements_share_it(self):
        labels = perf.unique_labels([("a1", "Sales"), ("b2", "Sales"), ("c3", "Costs")])
        self.assertEqual(labels, {"a1": "Sales [a1]", "b2": "Sales [b2]", "c3": "Costs"})


class ReadingFormulas(unittest.TestCase):
    def test_nested_calls_are_found_and_strings_and_references_are_skipped(self):
        names = [n for n, _ in perf.split_calls('If(Contains([A (x)], "Lookup(oops)"), Lower(RegexpExtract([B], "(a)")), 0)')]
        self.assertEqual(names, ["If", "Contains", "Lower", "RegexpExtract"])

    def test_cost_is_zero_for_plain_logic_and_grows_for_costly_functions(self):
        self.assertEqual(perf.formula_cost('If([A] > 1, "x", "y") + [B] * 2'), 0)
        self.assertGreater(perf.formula_cost("Lookup([T/C], [K], [T/K])"), perf.formula_cost('RegexpExtract([A], "x")'))
        self.assertGreaterEqual(perf.formula_cost("CallText(\"p\", [A])"), 50)

    def test_lookup_keys_are_split_correctly(self):
        self.assertEqual(perf.lookup_key_args("Lookup([T/V], [K], [T/K])"), [["[K]", "[T/K]"]])
        self.assertEqual(perf.lookup_key_args("Lookup([T/V], Text([K]), Lower(Trim([T/K])))"), [["Text([K])", "Lower(Trim([T/K]))"]])
        self.assertEqual(perf.lookup_key_args("Sum([A])"), [])


class Opinions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_sigma.install(blame)
        cls.d = blame.trace_workbook(fake_sigma.WB_ID, {})
        cls.recs = recommend.recommend(cls.d)
        cls.g = cls.d["graph"]

    def of_type(self, t):
        return [r for r in self.recs if r["type"] == t]

    def test_the_generated_sql_is_read_and_summarized(self):
        comp = self.d["compiled"]
        self.assertEqual(set(comp), {"eCust", "eOrd", "eExp", "eExp2", "eRev", "eRevSeg"})
        self.assertEqual(comp["eOrd"]["profile"]["joins"], 5)
        self.assertEqual(self.d["compiled_note"], "")
        for c in comp.values():
            self.assertNotIn("sql", c)

    def test_elements_that_rerun_the_same_join_stack_are_grouped(self):
        r = self.of_type("perf-shared-joins")
        self.assertEqual(len(r), 1)
        r = r[0]
        self.assertIn("Revenue Summary", r["detail"])
        self.assertIn("Revenue by Segment", r["detail"])
        self.assertEqual((r["confidence"], r["verdict"]), ("Measured", "May be faster"))
        self.assertIn("10 joins", r["evidence"])
        self.assertTrue(r["nodes"] and set(r["roles"].values()) == {"Its query re-runs the shared joins"})

    def test_the_one_element_that_is_not_on_the_stack_is_left_out(self):
        r = self.of_type("perf-shared-joins")[0]
        self.assertNotIn("Orders by Customer", r["detail"])

    def test_repeated_reads_of_one_table_are_reported(self):
        r = self.of_type("perf-repeated-scan")
        self.assertEqual(len(r), 1)
        self.assertIn("CUSTOMERS 5 times", r[0]["title"])
        self.assertEqual(r[0]["confidence"], "Measured")

    def test_lookups_that_are_not_provably_one_row_are_reported_as_a_hypothesis(self):
        r = self.of_type("perf-uncertain-lookups")
        self.assertEqual(len(r), 1)
        self.assertIn("5 values", r[0]["title"])
        self.assertIn("hypothesis", r[0]["action"])

    def test_a_key_wrapped_in_a_function_is_found_and_confirmed_by_the_sql(self):
        r = self.of_type("perf-wrapped-key")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["confidence"], "Measured")
        self.assertEqual({self.g[k]["c"] for k in r[0]["nodes"]}, {"Tier (text key)"})
        self.assertIn("1 lookup matches", r[0]["title"])

    def test_cheap_logic_shared_by_two_columns_is_not_reported_as_costly(self):
        # Size Upper and Size Lower both contain If([Amount] > 100, ...): that is cheap, so it is no performance finding
        flagged = {self.g[k]["c"] for r in self.of_type("perf-shared-subexpression") for k in r["nodes"]}
        self.assertNotIn("Size Upper", flagged)
        self.assertNotIn("Size Lower", flagged)

    def test_plain_key_lookups_are_not_called_wrapped(self):
        wrapped = self.of_type("perf-wrapped-key")[0]
        self.assertNotIn("Customer Segment", {self.g[k]["c"] for k in wrapped["nodes"]})

    def test_logic_repeated_across_columns_names_the_column_that_already_holds_it(self):
        recs = self.of_type("perf-shared-subexpression")
        self.assertEqual(len(recs), 2)                                   # one per export element
        r = recs[0]
        roles = {self.g[k]["c"]: v for k, v in r["roles"].items()}
        self.assertEqual(roles["Region Code"], "Already holds this logic: call it")
        self.assertEqual(roles["Region Prefix"], "Repeats it inside a larger formula")
        self.assertEqual((r["verdict"], r["confidence"]), ("Likely faster", "Measured"))

    def test_every_recommendation_has_a_verdict_a_confidence_and_a_reason(self):
        for r in self.recs:
            self.assertIn(r["verdict"], perf.VERDICTS, r["title"])
            self.assertIn(r["confidence"], ("Measured", "Inferred"), r["title"])
            self.assertTrue(r["evidence"] and r["verify"], r["title"])

    def test_tidying_that_does_not_change_the_query_is_called_maintainability(self):
        self.assertEqual({r["verdict"] for r in self.of_type("dead-column")}, {"Mostly maintainability"})
        order_size = [r for r in self.of_type("dup-formula") if "Order Size" in r["title"]][0]
        self.assertEqual(order_size["verdict"], "Mostly maintainability")     # cheap logic

    def test_lookups_that_become_joins_are_called_likely_faster_when_the_sql_confirms_it(self):
        self.assertEqual(self.of_type("multi-lookup")[0]["verdict"], "Likely faster")

    def test_the_summary_ranks_elements_and_lists_the_best_opportunities(self):
        pf = self.d["perf"]
        self.assertTrue(pf["available"])
        self.assertEqual(pf["elements"][0]["name"], "Orders by Customer")
        self.assertEqual(pf["totals"]["joins"], 15)
        self.assertLessEqual(len(pf["top"]), 5)
        by_id = {r["id"]: r for r in self.recs}
        self.assertTrue(pf["top"])
        for rid in pf["top"]:
            self.assertNotEqual(by_id[rid]["verdict"], "Mostly maintainability")

    def test_best_opportunities_are_ranked_by_verdict_then_estimated_saving(self):
        by_id = {r["id"]: r for r in self.recs}
        top = [by_id[i] for i in self.d["perf"]["top"]]
        order = {v: n for n, v in enumerate(perf.VERDICTS)}
        keys = [(order[r["verdict"]], -(r["impact"] or 0)) for r in top]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(all((r["impact"] or 0) > 0 for r in top))

    def test_maintainability_findings_have_no_estimated_saving(self):
        for r in self.recs:
            if r["verdict"] == "Mostly maintainability":
                self.assertEqual(r["impact"], 0, r["title"])

    def test_the_page_shows_the_outlook_and_verdicts(self):
        page = render_html(dict(self.d, recommendations=self.recs))
        self.assertIn("Performance outlook", page)
        self.assertIn("Likely faster", page)
        self.assertIn("perf-shared-joins", page)
        self.assertNotIn("SELECT", page.split("var PF=")[1][:3000])      # no raw SQL is embedded


class Calibration(unittest.TestCase):
    """The opinions must not overclaim: when the SQL shows the work is already shared, say so."""

    def setUp(self):
        fake_sigma.install(blame)
        self.real = fake_sigma.compiled_sql

    def tearDown(self):
        fake_sigma.compiled_sql = self.real

    def trace(self):
        d = blame.trace_workbook(fake_sigma.WB_ID, {})
        return d, recommend.recommend(d)

    def test_repeated_formula_logic_is_only_inferred_when_the_sql_computes_it_once(self):
        def once():
            sql = self.real()
            once_sql = ("select ORDER_ID, regexp_substr(REGION, '[A-Z]+') REGION_CODE_1, lower(REGION_CODE_1) REGION_PREFIX_2 "
                        "from ANALYTICS.MARTS.ORDERS limit 1000")
            sql["eExp"], sql["eExp2"] = once_sql, once_sql
            return sql
        fake_sigma.compiled_sql = once
        _, recs = self.trace()
        sub = [r for r in recs if r["type"] == "perf-shared-subexpression"]
        self.assertTrue(sub)
        for r in sub:
            self.assertEqual(r["confidence"], "Inferred")
            self.assertEqual(r["verdict"], "May be faster")
            self.assertIn("may already share", r["detail"])

    def test_without_generated_sql_nothing_measured_is_claimed(self):
        d = blame.trace_workbook(fake_sigma.WB_ID, {}, with_sql=False)
        recs = recommend.recommend(d)
        self.assertEqual(d["compiled"], {})
        self.assertIn("skipped", d["compiled_note"])
        self.assertFalse([r for r in recs if r["type"] in ("perf-shared-joins", "perf-repeated-scan", "perf-uncertain-lookups")])
        self.assertFalse([r for r in recs if r["confidence"] == "Measured"])
        for r in recs:
            self.assertIn(r["verdict"], perf.VERDICTS)
        self.assertFalse(d["perf"]["available"])
        self.assertIn("formulas only", render_html(dict(d, recommendations=recs)))

    def test_a_refused_queries_endpoint_degrades_gracefully(self):
        real_http = blame.http

        def refuse(path, data=None, headers=None, tries=4):
            if "/queries" in path:
                raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
            return real_http(path, data, headers, tries)
        blame.http = refuse
        try:
            d = blame.trace_workbook(fake_sigma.WB_ID, {})
        finally:
            blame.http = real_http
        self.assertEqual(d["compiled"], {})
        self.assertIn("HTTP 403", d["compiled_note"])
        self.assertTrue(d["elements"])                                   # the trace itself is unaffected

    def test_a_stack_that_already_reads_a_materialized_element_says_so(self):
        def with_mat():
            sql = self.real()
            mat = 'SIGMA_ON_SIGMA.SIGMA_WRITABLE."T_MAT_53DDA5DFD35E45FB9BC6E4E71DF7D54C_0"'
            sql["eRev"] = sql["eRev"].replace("ANALYTICS.MARTS.PRODUCTS", mat)
            sql["eRevSeg"] = sql["eRevSeg"].replace("ANALYTICS.MARTS.PRODUCTS", mat)
            return sql
        fake_sigma.compiled_sql = with_mat
        _, recs = self.trace()
        r = [r for r in recs if r["type"] == "perf-shared-joins"][0]
        self.assertIn("already reads materialized element T_MAT_53DDA5", r["detail"])
        self.assertIn("beyond it", r["detail"])

    def test_a_single_heavy_element_is_not_a_shared_stack(self):
        def one_heavy():
            sql = self.real()
            sql["eRevSeg"] = "select ORDER_ID from ANALYTICS.MARTS.ORDERS limit 10"      # only Revenue Summary stays heavy
            return sql
        fake_sigma.compiled_sql = one_heavy
        _, recs = self.trace()
        self.assertFalse([r for r in recs if r["type"] == "perf-shared-joins"])

    def test_elements_that_only_share_a_table_are_not_a_shared_join_stack(self):
        def single_table():
            sql = self.real()
            sql["eRev"] = sql["eRevSeg"] = "select ORDER_ID from ANALYTICS.MARTS.ORDERS limit 10"
            return sql
        fake_sigma.compiled_sql = single_table
        _, recs = self.trace()
        self.assertFalse([r for r in recs if r["type"] == "perf-shared-joins"])


if __name__ == "__main__":
    unittest.main()
