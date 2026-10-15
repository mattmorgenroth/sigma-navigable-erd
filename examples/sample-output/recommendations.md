# Duplicate-logic and performance recommendations: Sales Performance Overview

15 findings. Each is a heuristic; verify before changing anything.

## Performance outlook

Read the generated SQL for 6 elements: 15 joins in total, 8 repeated reads of a table inside one query, 5 values guarded against more than one match. The weights below rank elements against each other; they are not run times.

Findings by verdict: Likely faster: 3, May be faster: 6, Mostly maintainability: 6

Best opportunities, largest estimated saving first:

1. 4 lookups to Customers on the same key in Orders by Customer (Likely faster, Measured; 4 lookups → 1 join)
2. Orders Export: 2 columns repeat the same regex logic (Likely faster, Measured; 2 evaluations to 1)
3. Orders Export (copy): 2 columns repeat the same regex logic (Likely faster, Measured; 2 evaluations to 1)
4. Orders by Customer reads CUSTOMERS 5 times in one query (May be faster, Measured; up to 4 fewer reads)
5. 2 elements each run the same 4-table join stack (May be faster, Measured; about 5 fewer joins across 2 queries)

Heaviest elements (weight, joins, repeated reads):

- Orders by Customer: 33.5, 5 joins, 4 repeated reads
- Revenue Summary: 25, 5 joins, 2 repeated reads
- Revenue by Segment: 25, 5 joins, 2 repeated reads
- Orders Export: 2, 0 joins, 0 repeated reads
- Orders Export (copy): 2, 0 joins, 0 repeated reads
- Customers: 0, 0 joins, 0 repeated reads

## [HIGH] Orders Export and Orders Export (copy) repeat the same calculated columns
- Type: dup-element  |  Saves: 1 redundant element  |  Performance: May be faster (Measured)
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview) share at least 7 calculated columns with identical formulas (at least 100% overlap). Filters, sorting and layout are not compared, so they may still differ.
- Do: If the elements only differ by a filter, keep one and filter it with a control or a saved view. Otherwise move the shared calculated columns into one common source and point the others at it.
- Performance: these elements' generated queries total a cost index of 4 (a ranking weight, not seconds). How to check: Each element is its own query, so keeping one saves the other's query. Confirm with query history.

## [HIGH] Orders Export: 2 columns repeat the same regex logic
- Type: perf-shared-subexpression  |  Saves: 2 evaluations to 1  |  Performance: Likely faster (Measured)
- Where: Orders Export
- Owners to involve: Alex Rivera
- Why: In Orders Export, 2 columns contain RegexpExtract([Region], "[A-Z]+"). The generated SQL repeats the matching regex call 2 times.
- Do: Make the other columns reference the column that already holds it.
- Performance: repeated in the generated SQL. How to check: Look for the repeated call in the element's SQL; if it appears once, the compiler already shares it.

## [HIGH] Orders Export (copy): 2 columns repeat the same regex logic
- Type: perf-shared-subexpression  |  Saves: 2 evaluations to 1  |  Performance: Likely faster (Measured)
- Where: Orders Export (copy)
- Owners to involve: Alex Rivera
- Why: In Orders Export (copy), 2 columns contain RegexpExtract([Region], "[A-Z]+"). The generated SQL repeats the matching regex call 2 times.
- Do: Make the other columns reference the column that already holds it.
- Performance: repeated in the generated SQL. How to check: Look for the repeated call in the element's SQL; if it appears once, the compiler already shares it.

## [MED] Same formula written 3 times ('Order Size')
- Type: dup-formula  |  Saves: 3 copies → 1  |  Performance: Mostly maintainability (Inferred)
- Where: Orders by Customer (Sales Performance Overview); Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 3 elements: Orders by Customer (Sales Performance Overview), Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: If([Amount] > 1000, "Large", If([Amount] > 100, "Medium", "Small"))
- Do: Define it once in a shared upstream element or data model and reference it everywhere.
- Performance: cheap logic; each element's query repeats it. How to check: Cheap expressions rarely change run time. The gain is one place to edit.

## [MED] 'Customer Segment' is looked up from Customers but already available upstream
- Type: lookup  |  Saves: 1 lookup  |  Performance: May be faster (Measured)
- Where: Orders by Customer / Sales Performance Overview
- Owners to involve: Alex Rivera
- Why: In Orders by Customer (Sales Performance Overview), 'Customer Segment' uses Lookup([Customers/Segment], …). A column named 'SEGMENT' is already exposed by ORDERS (direct source); they trace to different source tables, so confirm the values match.
- Do: Reference [ORDERS/SEGMENT] directly and delete the lookup column. Check that 'SEGMENT' is the same value for every row before removing it.
- Performance: removes a join from the element's generated SQL. How to check: Confirm the two columns hold the same value, then compare the element's SQL.

## [MED] 4 lookups to Customers on the same key in Orders by Customer
- Type: multi-lookup  |  Saves: 4 lookups → 1 join  |  Performance: Likely faster (Measured)
- Where: Orders by Customer / Sales Performance Overview
- Owners to involve: Alex Rivera
- Why: 4 columns in Orders by Customer (Sales Performance Overview) each run Lookup against Customers using the same key (Customer Id, Customers/Customer Id). Values pulled: Country, Name, Segment, Tier.
- Do: Join Customers once (or add it as a relationship) and reference its columns, replacing 4 lookups.
- Performance: the element's generated SQL has 5 joins. How to check: Count the joins in the element's SQL before and after.

## [MED] Orders by Customer reads CUSTOMERS 5 times in one query
- Type: perf-repeated-scan  |  Saves: up to 4 fewer reads  |  Performance: May be faster (Measured)
- Where: Orders by Customer
- Owners to involve: Alex Rivera
- Why: The SQL for Orders by Customer reads CUSTOMERS 5 times. Each extra read is another scan of that table inside the same query.
- Do: Reduce the number of separate lookups or joins into this table: join it once and reference its columns, or look up one row of needed columns together. If it is a large table, this is the first place to look.
- Performance: CUSTOMERS appears 5 times in the generated SQL. How to check: Open the element's query profile in the warehouse and look for repeated scans of that table.

## [MED] 2 elements each run the same 4-table join stack
- Type: perf-shared-joins  |  Saves: about 5 fewer joins across 2 queries  |  Performance: May be faster (Measured)
- Where: Revenue Summary; Revenue by Segment
- Owners to involve: Alex Rivera
- Why: The SQL for Revenue Summary and Revenue by Segment joins the same 4 tables (CUSTOMERS, ORDERS, PRODUCTS and REGIONS), using 5 and 5 joins. Each element compiles to its own query that includes everything upstream, so the stack runs once per element, about 10 joins in all.
- Do: Do the joins once in the 'Revenue' element, which they all draw from, and have the others read its output. Materializing that element, or moving the joins into a data model or the warehouse, lets the others skip the work.
- Performance: 10 joins across 2 generated queries over 4 shared tables. How to check: Compare the query history for these elements before and after, or count the joins in each element's SQL.

## [MED] Orders by Customer: 5 values are guarded against more than one match
- Type: perf-uncertain-lookups  |  Saves: up to 5 guarded values  |  Performance: May be faster (Measured)
- Where: Orders by Customer
- Owners to involve: Alex Rivera
- Why: The SQL for Orders by Customer wraps 5 values in min/max with a null-safe comparison. That is how Sigma shows a value it cannot prove is a single value per key, whether it comes from a lookup or from a grouped table, and it adds work to every such column.
- Do: If they come from lookups, make the looked-up side unique on its key (group it by the key, or use a table whose key is declared unique) so the lookup compiles to a plain join. If they come from a grouped table, group only by the keys you need. This is a hypothesis: confirm the key really is unique first.
- Performance: 5 min/max null-equality guards in the generated SQL. How to check: Check that the key is unique in the source, then compare the element's SQL before and after.

## [MED] Orders by Customer: 1 lookup matches on a key wrapped in a function
- Type: perf-wrapped-key  |  Saves: 1 join to simplify  |  Performance: May be faster (Measured)
- Where: Orders by Customer
- Owners to involve: Alex Rivera
- Why: 1 Lookup column in Orders by Customer matches on a key that has a function or cast around it, and the generated SQL has 1 join condition with a cast. A transformed key hides the plain column from the optimizer, which can stop it from simplifying the join.
- Do: Compute the cleaned-up key once in a helper column on each side (or fix the type in the source) and look up on the plain column.
- Performance: 1 join condition with a cast in the generated SQL. How to check: Compare the join conditions in the element's SQL after switching to plain key columns.

## [LOW] 1 hidden calculated column in Orders by Customer is not used
- Type: dead-column  |  Saves: 1 column  |  Performance: Mostly maintainability (Inferred)
- Where: Orders by Customer / Sales Performance Overview
- Owners to involve: Alex Rivera
- Why: No formula, filter, sort, grouping or control in the workbook refers to: Scratch.
- Do: Delete them. First check they are not used by an action, a saved view, or something outside this workbook.
- Performance: unused columns are left out of the generated SQL. How to check: Not needed: removing them tidies the workbook but does not change the query.

## [LOW] Same formula written 2 times ('Order Year')
- Type: dup-formula  |  Saves: 2 copies → 1  |  Performance: Mostly maintainability (Inferred)
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 2 elements: Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: DateTrunc("year", [Order Date])
- Do: Define it once in a shared upstream element or data model and reference it everywhere.
- Performance: cheap logic; each element's query repeats it. How to check: Cheap expressions rarely change run time. The gain is one place to edit.

## [LOW] Same formula written 2 times ('Margin')
- Type: dup-formula  |  Saves: 2 copies → 1  |  Performance: Mostly maintainability (Inferred)
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 2 elements: Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: ([Amount] - [Cost]) / [Amount]
- Do: Define it once in a shared upstream element or data model and reference it everywhere.
- Performance: cheap logic; each element's query repeats it. How to check: Cheap expressions rarely change run time. The gain is one place to edit.

## [LOW] Same formula written 2 times ('Region Code')
- Type: dup-formula  |  Saves: 2 copies → 1  |  Performance: Mostly maintainability (Inferred)
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 2 elements: Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: RegexpExtract([Region], "[A-Z]+")
- Do: Define it once in a shared upstream element or data model and reference it everywhere.
- Performance: cheap logic; each element's query repeats it. How to check: Cheap expressions rarely change run time. The gain is one place to edit.

## [LOW] Same formula written 2 times ('Region Prefix')
- Type: dup-formula  |  Saves: 2 copies → 1  |  Performance: Mostly maintainability (Inferred)
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 2 elements: Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: Lower(RegexpExtract([Region], "[A-Z]+"))
- Do: Define it once in a shared upstream element or data model and reference it everywhere.
- Performance: cheap logic; each element's query repeats it. How to check: Cheap expressions rarely change run time. The gain is one place to edit.
