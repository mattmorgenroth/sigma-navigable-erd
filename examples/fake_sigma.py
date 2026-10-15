"""A tiny fake Sigma API with an entirely fictional "Acme Retail" workbook.

Used by the tests and by examples/offline_demo.py so the real tracer can run end to end with no network,
no credentials and no real company data. Everything here is invented.

The fixture deliberately contains each kind of duplicated logic the recommendation engine looks for:
  * a lookup of a column ("Segment") that the element's own source table already has
  * four lookups to the same source on the same key (should be one join)
  * the same formula ("Order Size") written in three elements
  * two elements ("Orders Export" and its copy) that repeat the same five calculated columns
  * a hidden calculated column that nothing uses ("Scratch")
  * a lookup that matches on a key wrapped in a function, and repeated regular-expression logic in two columns
  * two summary elements whose generated SQL re-runs the same four-table join stack
  The fake API also serves invented compiled SQL for every element, so the performance opinions can be exercised.
It also contains things that must NOT be flagged: pass-through columns and a clean customer element.
"""
import base64
import hashlib
import json
import re
import sys
import urllib.parse

import yaml

ORG = "acme"
CONN = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WB_ID = "11111111-1111-4111-8111-111111111111"
DM_REV = "22222222-2222-4222-8222-222222222222"
DM_CUST = "33333333-3333-4333-8333-333333333333"
WB_URLID = "wbAAAA1111"
BASE_URL = f"https://app.sigmacomputing.com/{ORG}"
WB_URL = f"{BASE_URL}/workbook/Sales-Performance-Overview-{WB_URLID}"

MEMBERS = {
    "m-alex": ("Alex", "Rivera", "alex.rivera@example.com"),
    "m-priya": ("Priya", "Shah", "priya.shah@example.com"),
    "m-sam": ("Sam", "Okafor", "sam.okafor@example.com"),
    "m-jo": ("Jo", "Lindqvist", "jo.lindqvist@example.com"),
}

# warehouse tables as the API reports them (upper-case names)
TABLES = {
    "ANALYTICS.MARTS.ORDERS": ["ORDER_ID", "ORDER_DATE", "REGION", "CUSTOMER_ID", "SEGMENT", "AMOUNT", "DISCOUNT", "COST"],
    "ANALYTICS.MARTS.CUSTOMERS": ["CUSTOMER_ID", "NAME", "SEGMENT", "TIER", "COUNTRY", "REGION"],
}


def wh(table):
    return {"kind": "warehouse-table", "connectionId": CONN, "path": ["ANALYTICS", "MARTS", table]}


def col(cid, name, formula, hidden=False):
    c = {"id": cid, "name": name, "formula": formula}
    if hidden:
        c["hidden"] = True
    return c


ORDER_SIZE = 'If([Amount] > 1000, "Large", If([Amount] > 100, "Medium", "Small"))'


def export_columns(prefix):
    """Columns shared by 'Orders Export' and its copy: 5 identical calculated columns plus plain ones."""
    return [
        col(f"{prefix}01", "Order Id", "[ORDERS/Order Id]"),
        col(f"{prefix}02", "Order Date", "[ORDERS/Order Date]"),
        col(f"{prefix}03", "Amount", "[ORDERS/Amount]"),
        col(f"{prefix}04", "Discount", "[ORDERS/Discount]"),
        col(f"{prefix}05", "Cost", "[ORDERS/Cost]"),
        col(f"{prefix}06", "Order Size", ORDER_SIZE),
        col(f"{prefix}07", "Net Amount", "[Amount] - [Discount]"),
        col(f"{prefix}08", "Is Large", "[Amount] > 1000"),
        col(f"{prefix}09", "Order Year", 'DateTrunc("year", [Order Date])'),
        col(f"{prefix}10", "Margin", "([Amount] - [Cost]) / [Amount]"),
        col(f"{prefix}11", "Region", "[ORDERS/Region]"),
        col(f"{prefix}12", "Region Code", 'RegexpExtract([Region], "[A-Z]+")'),
        col(f"{prefix}13", "Region Prefix", 'Lower(RegexpExtract([Region], "[A-Z]+"))'),
    ]


WORKBOOK = {
    "workbookId": WB_ID, "name": "Sales Performance Overview", "url": WB_URL, "ownerId": "m-alex",
    "document": {"elements": [
        {"id": "eCust", "kind": "table", "name": "Customers",
         "source": {"kind": "data-model", "dataModelId": DM_CUST, "elementId": "dCust"},
         "columns": [col("cu01", "Customer Id", "[Customer Dimension/Customer Id]"),
                     col("cu02", "Name", "[Customer Dimension/Name]"),
                     col("cu03", "Segment", "[Customer Dimension/Segment]"),
                     col("cu04", "Tier", "[Customer Dimension/Tier]"),
                     col("cu05", "Country", "[Customer Dimension/Country]")]},
        {"id": "eOrd", "kind": "table", "name": "Orders by Customer", "source": wh("ORDERS"),
         "columns": [col("or01", "Order Id", "[ORDERS/Order Id]"),
                     col("or02", "Order Date", "[ORDERS/Order Date]"),
                     col("or03", "Customer Id", "[ORDERS/Customer Id]"),
                     col("or04", "Amount", "[ORDERS/Amount]"),
                     col("or05", "Order Size", ORDER_SIZE),
                     col("or06", "Customer Segment", "Lookup([Customers/Segment], [Customer Id], [Customers/Customer Id])"),
                     col("or07", "Customer Name", "Lookup([Customers/Name], [Customer Id], [Customers/Customer Id])"),
                     col("or08", "Customer Tier", "Lookup([Customers/Tier], [Customer Id], [Customers/Customer Id])"),
                     col("or09", "Customer Country", "Lookup([Customers/Country], [Customer Id], [Customers/Customer Id])"),
                     col("or11", "Size Upper", 'Upper(If([Amount] > 100, "Big", "Small"))'),
                     col("or12", "Size Lower", 'Lower(If([Amount] > 100, "Big", "Small"))'),
                     col("or10", "Tier (text key)", "Lookup([Customers/Tier], Text([Customer Id]), Text([Customers/Customer Id]))"),
                     col("scratch01", "Scratch", "[Amount] * 2", hidden=True)]},
        {"id": "eExp", "kind": "table", "name": "Orders Export", "source": wh("ORDERS"), "columns": export_columns("ex")},
        {"id": "eExp2", "kind": "table", "name": "Orders Export (copy)", "source": wh("ORDERS"), "columns": export_columns("ey")},
        {"id": "eRev", "kind": "table", "name": "Revenue Summary",
         "source": {"kind": "data-model", "dataModelId": DM_REV, "elementId": "dRev"},
         "columns": [col("rv01", "Order Id", "[Revenue/Order Id]"),
                     col("rv02", "Net Revenue", "[Revenue/Net Revenue]"),
                     col("rv03", "Revenue per Order", "Sum([Net Revenue]) / CountDistinct([Order Id])")]},
        {"id": "eRevSeg", "kind": "table", "name": "Revenue by Segment",
         "source": {"kind": "data-model", "dataModelId": DM_REV, "elementId": "dRev"},
         "columns": [col("rs01", "Order Id", "[Revenue/Order Id]"),
                     col("rs02", "Net Revenue", "[Revenue/Net Revenue]"),
                     col("rs03", "Average Net Revenue", "Avg([Net Revenue])")]},
    ]},
}

DM_REVENUE = {
    "dataModelId": DM_REV, "name": "Revenue Model", "url": f"{BASE_URL}/data-model/Revenue-Model-dmBBBB2222", "ownerId": "m-priya",
    "pages": [{"id": "p1", "name": "Model", "elements": [
        {"id": "dRev", "kind": "table", "name": "Revenue", "source": wh("ORDERS"),
         "columns": [col("rm01", "Order Id", "[ORDERS/Order Id]"),
                     col("rm02", "Amount", "[ORDERS/Amount]"),
                     col("rm03", "Discount", "[ORDERS/Discount]"),
                     col("rm04", "Net Revenue", "[ORDERS/Amount] - [ORDERS/Discount]")]}]}],
}

DM_CUSTOMER = {
    "dataModelId": DM_CUST, "name": "Customer Dimension", "url": f"{BASE_URL}/data-model/Customer-Dimension-dmCCCC3333", "ownerId": "m-sam",
    "pages": [{"id": "p1", "name": "Model", "elements": [
        {"id": "dCust", "kind": "table", "name": "Customer Dimension", "source": wh("CUSTOMERS"),
         "columns": [col("cd01", "Customer Id", "[CUSTOMERS/Customer Id]"),
                     col("cd02", "Name", "[CUSTOMERS/Name]"),
                     col("cd03", "Segment", 'If(IsNull([CUSTOMERS/Segment]), "Unknown", [CUSTOMERS/Segment])'),
                     col("cd04", "Tier", "[CUSTOMERS/Tier]"),
                     col("cd05", "Country", "[CUSTOMERS/Country]")]}]}],
}

OBJECTS = {  # id -> (spec, last editor)
    WB_ID: (WORKBOOK, "m-alex"),
    DM_REV: (DM_REVENUE, "m-priya"),
    DM_CUST: (DM_CUSTOMER, "m-jo"),
}

LINEAGE = {
    WB_ID: [{"type": "data-model", "dataModelId": DM_CUST, "name": "Customer Dimension", "connectionId": CONN},
            {"type": "data-model", "dataModelId": DM_REV, "name": "Revenue", "connectionId": CONN},
            {"type": "table", "inodeId": "inode-ORDERS", "name": "ORDERS"}],
    DM_REV: [{"type": "table", "inodeId": "inode-ORDERS", "name": "ORDERS"}],
    DM_CUST: [{"type": "table", "inodeId": "inode-CUSTOMERS", "name": "CUSTOMERS"}],
}


API_HOST = "https://api.sigmacomputing.com"
CLIENT_ID, CLIENT_SECRET = "fake-client-id", "fake-client-secret"


class FakeAuth:
    """A fake Sigma authorization server: discovery, dynamic registration, PKCE, refresh-token rotation, whoami."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.valid_access, self.refresh_tokens, self.codes, self.clients = set(), {}, {}, {}
        self.n = 0
        self.log = []                       # (method, url) of every request, so tests can assert on them
        self.offer_discovery = True         # False makes whoami answer 401 without a WWW-Authenticate header
        self.allow_registration = True
        self.reject_token_requests = False

    def _issue(self, client_id):
        self.n += 1
        access, refresh = f"access-{self.n}", f"refresh-{self.n}"
        self.valid_access.add(access)
        self.refresh_tokens[refresh] = client_id
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600}

    def issue_code(self, client_id, redirect, challenge):
        self.n += 1
        code = f"code-{self.n}"
        self.codes[code] = (client_id, redirect, challenge)
        return code

    def request(self, method, url, headers=None, data=None, form=None, json_body=None, timeout=30):
        from auth import check_host
        check_host(url)                                    # the real code must never leave sigmacomputing.com
        self.log.append((method, url))
        u = urllib.parse.urlparse(url)
        p, h = u.path, {k.lower(): v for k, v in (headers or {}).items()}
        out = lambda status, body=None, hdrs=None: (status, {k.lower(): v for k, v in (hdrs or {}).items()},
                                                    json.dumps(body) if body is not None else "")
        if p == "/v2/whoami":
            if h.get("authorization", "").removeprefix("Bearer ") in self.valid_access:
                return out(200, {"userId": "m-alex", "organizationId": ORG})
            hdr = {"WWW-Authenticate": f'Bearer resource_metadata="{API_HOST}/v2/.well-known/oauth-protected-resource", scope="api:access"'}
            return out(401, {"message": "unauthorized"}, hdr if self.offer_discovery else {})
        if p == "/v2/.well-known/oauth-protected-resource":
            return out(200, {"resource": API_HOST + "/v2", "authorization_servers": [API_HOST], "scopes_supported": ["api:access"]})
        if p == "/.well-known/oauth-authorization-server":
            return out(200, {"issuer": API_HOST, "authorization_endpoint": API_HOST + "/mcp/authorize",
                             "token_endpoint": API_HOST + "/mcp/token", "registration_endpoint": API_HOST + "/mcp/register",
                             "code_challenge_methods_supported": ["S256"], "response_types_supported": ["code"],
                             "grant_types_supported": ["authorization_code", "refresh_token"],
                             "token_endpoint_auth_methods_supported": ["none"]})
        if p == "/mcp/register":
            if not self.allow_registration:
                return out(403, {"error": "registration_disabled"})
            cid = f"client-{len(self.clients) + 1}"
            self.clients[cid] = json_body
            return out(201, {"client_id": cid})
        if p == "/mcp/token":
            if self.reject_token_requests:
                return out(400, {"error": "invalid_grant"})
            if form["grant_type"] == "authorization_code":
                cid, redirect, challenge = self.codes.pop(form["code"], (None, None, None))
                verifier_ok = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode() == challenge
                if cid != form["client_id"] or redirect != form["redirect_uri"] or not verifier_ok:
                    return out(400, {"error": "invalid_grant"})
                return out(200, self._issue(cid))
            if form["grant_type"] == "refresh_token":
                cid = self.refresh_tokens.pop(form["refresh_token"], None)     # single use: it rotates
                if cid is None or cid != form["client_id"]:
                    return out(400, {"error": "invalid_grant"})
                return out(200, self._issue(cid))
        if p == "/v2/auth/token":
            if form["grant_type"] == "client_credentials" and (form["client_id"], form["client_secret"]) == (CLIENT_ID, CLIENT_SECRET):
                self.n += 1
                self.valid_access.add(f"cc-{self.n}")
                return out(200, {"access_token": f"cc-{self.n}", "token_type": "Bearer", "expires_in": 3600})
            return out(401, {"message": "bad credentials"})
        raise KeyError(f"fake authorization server has no route for {method} {url}")


AUTH = FakeAuth()


def _stack_sql(cols):
    """Invented SQL for a summary element that re-expands a four-table join stack (five joins)."""
    return ("select Q1.ORDER_ID, Q1.AMOUNT - Q1.DISCOUNT NET_REVENUE_1 " + cols +
            " from ANALYTICS.MARTS.ORDERS Q1 left join ANALYTICS.MARTS.CUSTOMERS Q2 on Q1.CUSTOMER_ID = Q2.CUSTOMER_ID"
            " left join ANALYTICS.MARTS.REGIONS Q3 on Q1.REGION = Q3.REGION"
            " left join ANALYTICS.MARTS.PRODUCTS Q4 on Q1.ORDER_ID = Q4.ORDER_ID"
            " left join (select CUSTOMER_ID, count(*) N from ANALYTICS.MARTS.CUSTOMERS group by CUSTOMER_ID) Q5 on Q1.CUSTOMER_ID = Q5.CUSTOMER_ID"
            " left join ANALYTICS.MARTS.REGIONS Q6 on Q2.REGION = Q6.REGION limit 1000")


def compiled_sql():
    """element id -> the SQL the fake compiler 'generated'. Invented, but shaped like the real thing."""
    lookup = ("left join (select CUSTOMER_ID K, min({c}) MIN_{n}, max({c}) MAX_{n} from ANALYTICS.MARTS.CUSTOMERS group by CUSTOMER_ID) Q{n} "
              "on Q1.CUSTOMER_ID = Q{n}.K")
    ord_sql = ("select Q1.ORDER_ID, Q1.AMOUNT, " +
               ", ".join(f"iff(equal_null(min(Q{n}.MIN_{n}), max(Q{n}.MAX_{n})), max(Q{n}.MAX_{n}), null) V_{n}" for n in range(2, 7)) +
               " from ANALYTICS.MARTS.ORDERS Q1 " +
               " ".join(lookup.format(c=c, n=n) for n, c in zip(range(2, 6), ("SEGMENT", "NAME", "TIER", "COUNTRY"))) +
               " left join (select CUSTOMER_ID::text K, min(TIER) MIN_6, max(TIER) MAX_6 from ANALYTICS.MARTS.CUSTOMERS group by 1) Q6 "
               "on Q1.CUSTOMER_ID::text = Q6.K limit 1000")
    export = ("select ORDER_ID, AMOUNT, regexp_substr(REGION, '[A-Z]+') REGION_CODE_1, lower(regexp_substr(REGION, '[A-Z]+')) REGION_PREFIX_2, "
              "case when AMOUNT > 1000 then 'Large' else 'Small' end ORDER_SIZE_3 from ANALYTICS.MARTS.ORDERS limit 1000")
    return {
        "eCust": "select CUSTOMER_ID, NAME, SEGMENT, TIER, COUNTRY from ANALYTICS.MARTS.CUSTOMERS limit 1000",
        "eOrd": ord_sql, "eExp": export, "eExp2": export,
        "eRev": _stack_sql(", sum(Q1.AMOUNT) over (partition by Q1.REGION) PER_REGION_4"),
        "eRevSeg": _stack_sql(", avg(Q1.AMOUNT) over () AVG_5"),
    }


def fake_http(path, data=None, headers=None, tries=4):
    """Drop-in replacement for blame.http that answers from the fixture above."""
    u = urllib.parse.urlparse(path)
    p = u.path
    token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
    if token not in AUTH.valid_access:
        raise PermissionError(f"the tracer called {p} without a valid token")
    if p == "/v2/whoami":
        return json.dumps({"organizationId": ORG, "userId": "m-alex"})
    m = re.fullmatch(r"/v2/members/([\w-]+)", p)
    if m:
        f, l, e = MEMBERS[m.group(1)]
        return json.dumps({"memberId": m.group(1), "firstName": f, "lastName": l, "email": e})
    m = re.fullmatch(r"/v2/workbooks/([\w-]+)/queries", p)
    if m:
        names = {e["id"]: e["name"] for e in WORKBOOK["document"]["elements"]}
        return json.dumps({"entries": [{"elementId": k, "name": names[k], "sql": v + "\n\n-- Sigma {\"request-id\":\"fake\"}"}
                                       for k, v in compiled_sql().items()]})
    m = re.fullmatch(r"/v2/(workbooks|dataModels)/([\w-]+)/(spec|lineage)", p)
    if m:
        oid = m.group(2)
        if m.group(3) == "spec":
            return yaml.safe_dump(OBJECTS[oid][0], sort_keys=False)
        return json.dumps({"entries": LINEAGE.get(oid, [])})
    m = re.fullmatch(r"/v2/(workbooks|dataModels)/([\w-]+)", p)
    if m:
        spec, editor = OBJECTS[m.group(2)]
        return json.dumps({"name": spec["name"], "url": spec["url"], "ownerId": spec["ownerId"], "updatedBy": editor})
    if p == "/v2/files":
        return json.dumps({"entries": [{"id": WB_ID, "urlId": WB_URLID, "name": WORKBOOK["name"], "type": "workbook"}]})
    m = re.fullmatch(r"/v2/connection/([\w-]+)/lookup", p)
    if m:
        dotted = ".".join(json.loads(data)["path"])
        return json.dumps({"inodeId": "inode-" + dotted.split(".")[-1]}) if dotted in TABLES else json.dumps({})
    m = re.fullmatch(r"/v2/connections/tables/inode-(\w+)/columns", p)
    if m:
        for dotted, cols in TABLES.items():
            if dotted.endswith("." + m.group(1)):
                return json.dumps({"entries": [{"name": c} for c in cols]})
    raise KeyError(f"fake Sigma API has no route for {path}")


def install(blame_module):
    """Point the tracer and the sign-in code at this fake API. Returns the base URL to use.

    Sign-in sessions live in memory, so a test or demo never touches your real keychain. The default setup offers
    API client credentials, so the demo uses the fallback path; the OAuth tests drive the browser path directly.
    """
    import os
    import auth
    AUTH.reset()
    STORE.data.clear()
    blame_module.http = fake_http
    auth._request = AUTH.request
    auth.backend = lambda: STORE
    os.environ["SIGMA_CLIENT_ID"] = CLIENT_ID
    os.environ["SIGMA_CLIENT_SECRET"] = CLIENT_SECRET
    os.environ["SIGMA_BASE_URL"] = API_HOST
    for k in ("SIGMA_API_TOKEN", "SIGMA_AUTH_MODE"):
        os.environ.pop(k, None)
    blame_module.configure(API_HOST)
    blame_module.set_auth_mode(None)
    return API_HOST


class _Store:
    """In-memory stand-in for the OS keychain."""
    def __init__(self):
        self.data = {}

    def get(self, account):
        return self.data.get(account)

    def set(self, account, value):
        self.data[account] = value

    def delete(self, account):
        self.data.pop(account, None)


STORE = _Store()


if __name__ == "__main__":
    sys.exit("This module is a fixture. Run examples/offline_demo.py instead.")
