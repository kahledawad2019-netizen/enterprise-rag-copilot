---
doc_id: DOC-KPI-001
title: KPI and Business Metric Glossary
doc_type: kpi_glossary
version: "2.0"
effective_date: 2025-01-01
status: current
authority: standard
department: Finance
owner: VP Finance
access_group: internal
tenant: all
related_docs: [DOC-CHN-001, DOC-HLT-001]
tags: [kpi, mrr, arr, churn, metrics, definitions, glossary]
---

# KPI and Business Metric Glossary

**Northwind Cloud** - Version 2.0 - Effective 1 January 2025

This glossary is the written counterpart of the machine-readable definitions in
the `ai.business_glossary` table. Where the two ever disagree, this document is
authoritative and the table must be corrected.

## Revenue metrics

### MRR (Monthly Recurring Revenue)

The normalised monthly value of all paid, non-trial subscriptions active at any
point in the month. **Annual contracts are spread across the twelve months they
cover**, not recognised in the month they are invoiced.

Excludes: trials, professional-services fees, usage overage, and tax.

> **Changed in version 2.0.** Before 1 January 2025, MRR included trial
> subscriptions, which overstated the figure by roughly 4 percent. Comparisons
> across that boundary must be restated.

### ARR (Annual Recurring Revenue)

MRR multiplied by twelve. A forward-looking run rate, not billed or collected
revenue. ARR is never reduced for expected churn.

### New business MRR

MRR from customers acquired in the period who had no prior paid subscription.
Reactivated customers are counted separately and are not new business.

### Expansion revenue

Additional MRR from existing customers through upgrades or seat increases.

### Contraction revenue

MRR lost from existing customers who downgraded or reduced seats but did not
cancel. Reported as a positive number representing the amount lost.

## Customer metrics

### Active customer

A customer with at least one active, non-trial subscription and no churn date.
Status alone is not sufficient: a customer can be flagged active while every
subscription has lapsed.

### Trial customer

A customer whose only subscriptions are trials. Excluded from every revenue
metric and from churn denominators.

### Churned customer

A customer who has cancelled every paid subscription. Churn is recognised on
the subscription end date, not on the date notice was given.

## Churn metrics

### Logo churn

Customers lost in a period divided by customers active at the start.

### Revenue churn

MRR lost to cancellations divided by MRR at the start of the period. Also
called gross MRR churn. **It is never netted against expansion** - that would
be net revenue retention, a different metric.

## Billing metrics

### Overdue invoice

An invoice past its due date that is not paid in full. The outstanding figure
is the total minus the amount paid, not the total. An invoice due today is not
yet overdue.

### Refund rate

Total refunded divided by total billed over the same period. Excludes SLA
service credits, which are tracked against incidents rather than as refunds.

## Support metrics

### First-response time

Minutes between ticket open and the first **human** agent response. Automated
acknowledgements do not stop the clock. A ticket that never received a response
is a breach, not a zero.

### Resolution time

Minutes between ticket open and resolution. A ticket closed without being
resolved has no resolution time and must be excluded rather than counted as
zero.

### SLA breach

A ticket whose first response or resolution exceeded the target in the SLA
version **in force on the date the ticket was opened**. Judging a historical
ticket against the current SLA is the most common reporting error.
