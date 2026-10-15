# Duplicate-logic recommendations: Sales Performance Overview

7 findings. Each is a heuristic; verify before removing.

## [HIGH] Orders Export and Orders Export (copy) repeat the same calculated columns
- Type: dup-element  |  Saves: 1 redundant element
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview) share at least 5 calculated columns with identical formulas (at least 100% overlap). Filters, sorting and layout are not compared, so they may still differ.
- Do: If the elements only differ by a filter, keep one and filter it with a control or a saved view. Otherwise move the shared calculated columns into one common source and point the others at it.

## [MED] Same formula written 3 times ('Order Size')
- Type: dup-formula  |  Saves: 3 copies → 1
- Where: Orders by Customer (Sales Performance Overview); Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 3 elements: Orders by Customer (Sales Performance Overview), Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: If([Amount] > 1000, "Large", If([Amount] > 100, "Medium", "Small"))
- Do: Define it once in a shared upstream element or data model and reference it everywhere.

## [MED] 'Customer Segment' is looked up from Customers but already available upstream
- Type: lookup  |  Saves: 1 lookup
- Where: Orders by Customer / Sales Performance Overview
- Owners to involve: Alex Rivera
- Why: In Orders by Customer (Sales Performance Overview), 'Customer Segment' uses Lookup([Customers/Segment], …). A column named 'SEGMENT' is already exposed by ORDERS (direct source); they trace to different source tables, so confirm the values match.
- Do: Reference [ORDERS/SEGMENT] directly and delete the lookup column. Check that 'SEGMENT' is the same value for every row before removing it.

## [MED] 4 lookups to Customers on the same key in Orders by Customer
- Type: multi-lookup  |  Saves: 4 lookups → 1 join
- Where: Orders by Customer / Sales Performance Overview
- Owners to involve: Alex Rivera
- Why: 4 columns in Orders by Customer (Sales Performance Overview) each run Lookup against Customers using the same key (Customer Id, Customers/Customer Id). Values pulled: Country, Name, Segment, Tier.
- Do: Join Customers once (or add it as a relationship) and reference its columns, replacing 4 lookups.

## [LOW] 1 hidden calculated column in Orders by Customer is not used
- Type: dead-column  |  Saves: 1 column
- Where: Orders by Customer / Sales Performance Overview
- Owners to involve: Alex Rivera
- Why: No formula, filter, sort, grouping or control in the workbook refers to: Scratch.
- Do: Delete them. First check they are not used by an action, a saved view, or something outside this workbook.

## [LOW] Same formula written 2 times ('Order Year')
- Type: dup-formula  |  Saves: 2 copies → 1
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 2 elements: Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: DateTrunc("year", [Order Date])
- Do: Define it once in a shared upstream element or data model and reference it everywhere.

## [LOW] Same formula written 2 times ('Margin')
- Type: dup-formula  |  Saves: 2 copies → 1
- Where: Orders Export (Sales Performance Overview); Orders Export (copy) (Sales Performance Overview)
- Owners to involve: Alex Rivera
- Why: Identical logic appears in 2 elements: Orders Export (Sales Performance Overview), Orders Export (copy) (Sales Performance Overview). Formula: ([Amount] - [Cost]) / [Amount]
- Do: Define it once in a shared upstream element or data model and reference it everywhere.
