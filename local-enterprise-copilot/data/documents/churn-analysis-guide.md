---
doc_id: DOC-CHN-001
title: Churn Analysis Guide
doc_type: churn_guide
version: "1.3"
effective_date: 2025-09-15
status: current
authority: guidance
department: Revenue Operations
owner: Director of RevOps
access_group: internal
tenant: all
related_docs: [DOC-KPI-001, DOC-HLT-001, DOC-PM-2025-0042]
tags: [churn, analysis, retention, cohort, q2 2025]
---

# Churn Analysis Guide

**Northwind Cloud** - Version 1.3 - Effective 15 September 2025

## 1. Always report both churn measures

1.1 Logo churn and revenue churn can move in opposite directions. Losing ten
small customers and losing one large one are not the same event, and a single
number hides which happened.

1.2 Report both, always, with the denominator stated.

## 2. Standard investigation sequence

When churn rises, work through these in order:

1. **Segment it.** Is it concentrated in one tier, region or product?
2. **Check cohort age.** Early-life churn indicates an onboarding problem;
   late-life churn indicates a value problem.
3. **Check incidents.** Correlate the churn window against the incident record.
4. **Check support.** Look at SLA breach counts for the churned accounts.
5. **Check adoption.** Low seat adoption almost always precedes churn.
6. **Read the cancellation reasons.** They are free text and under-used.

## 3. The Q2 2025 spike

3.1 Churn in the second quarter of 2025 ran roughly 2.5 times the baseline.

3.2 The dominant cause was incident INC-2025-0042, the multi-region
authentication outage of 14 June 2025, which affected 99 customers and caused a
full outage for Enterprise accounts. See the postmortem, DOC-PM-2025-0042.

3.3 Secondary factors: a cluster of affected accounts were already showing low
adoption, and several had open SLA breaches before the incident. The incident
was the trigger, not the sole cause.

3.4 **Lesson recorded:** accounts with pre-existing low health are far more
likely to churn after an incident than healthy accounts experiencing the same
outage. Incident response should prioritise proactive contact with low-health
accounts, not only the largest ones.

## 4. Common analytical mistakes

4.1 Using the current SLA version to judge historical tickets.

4.2 Counting trial expiries as churn. They were never paid customers.

4.3 Netting expansion against churn and calling the result churn.

4.4 Measuring churn by cancellation notice date rather than subscription end
date, which shifts the spike into the wrong month.
