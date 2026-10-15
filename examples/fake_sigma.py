"""A tiny fake Sigma API with an entirely fictional "Acme Retail" workbook.

Used by the tests and by examples/offline_demo.py so the real tracer can run end to end with no network,
no credentials and no real company data. Everything here is invented.

The fixture deliberately contains each kind of duplicated logic the recommendation engine looks for:
  * a lookup of a column ("Segment") that the element's own source table already has
  * four lookups to the same source on the same key (should be one join)
  * the same formula ("Order Size") written in three elements
  * two elements ("Orders Export" and its copy) that repeat the same five calculated columns
  * a hidden calculated column that nothing uses ("Scratch")
It also contains things that must NOT be flagged: pass-through columns and a clean customer element.
"""
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
                     col("scratch01", "Scratch", "[Amount] * 2", hidden=True)]},
        {"id": "eExp", "kind": "table", "name": "Orders Export", "source": wh("ORDERS"), "columns": export_columns("ex")},
        {"id": "eExp2", "kind": "table", "name": "Orders Export (copy)", "source": wh("ORDERS"), "columns": export_columns("ey")},
        {"id": "eRev", "kind": "table", "name": "Revenue Summary",
         "source": {"kind": "data-model", "dataModelId": DM_REV, "elementId": "dRev"},
         "columns": [col("rv01", "Order Id", "[Revenue/Order Id]"),
                     col("rv02", "Net Revenue", "[Revenue/Net Revenue]"),
                     col("rv03", "Revenue per Order", "Sum([Net Revenue]) / CountDistinct([Order Id])")]},
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


def fake_http(path, data=None, headers=None, tries=4):
    """Drop-in replacement for blame.http that answers from the fixture above."""
    u = urllib.parse.urlparse(path)
    p = u.path
    if p == "/v2/auth/token":
        return json.dumps({"access_token": "fake-token"})
    if p == "/v2/whoami":
        return json.dumps({"organizationId": ORG, "userId": "m-alex"})
    m = re.fullmatch(r"/v2/members/([\w-]+)", p)
    if m:
        f, l, e = MEMBERS[m.group(1)]
        return json.dumps({"memberId": m.group(1), "firstName": f, "lastName": l, "email": e})
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
    """Point the tracer at this fake API. Returns the base URL to use."""
    import os
    blame_module.http = fake_http
    os.environ.setdefault("SIGMA_CLIENT_ID", "fake-client-id")
    os.environ.setdefault("SIGMA_CLIENT_SECRET", "fake-client-secret")
    os.environ.setdefault("SIGMA_BASE_URL", "https://api.sigmacomputing.com")
    blame_module.configure(os.environ["SIGMA_BASE_URL"])
    blame_module._tok = None
    return os.environ["SIGMA_BASE_URL"]


if __name__ == "__main__":
    sys.exit("This module is a fixture. Run examples/offline_demo.py instead.")
