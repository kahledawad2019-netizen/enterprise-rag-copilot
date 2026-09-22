---
doc_id: DOC-HLT-001
title: Customer Health Score Methodology
doc_type: health_methodology
version: "1.2"
effective_date: 2025-03-01
status: current
authority: guidance
department: Customer Success
owner: Director of Customer Success
access_group: internal
tenant: all
related_docs: [DOC-KPI-001, DOC-CHN-001, DOC-ONB-001]
tags: [health score, risk, churn prediction, adoption, at-risk]
---

# Customer Health Score Methodology

**Northwind Cloud** - Version 1.2 - Effective 1 March 2025

## 1. Composite score

The health score runs from 0 to 100 and is recomputed monthly:

| Component | Weight | Based on |
|---|---|---|
| Usage | 40 percent | Active users against purchased seats over 30 days |
| Billing | 30 percent | Overdue invoice count and payment lateness |
| Support | 30 percent | Ticket volume and SLA breach count |

## 2. Risk bands

| Score | Band | Expected action |
|---|---|---|
| 75 - 100 | Low | Standard cadence |
| 55 - 74 | Medium | Quarterly review |
| 35 - 54 | High | Monthly review, success plan required |
| 0 - 34 | Critical | Executive escalation within 5 business days |

## 3. At-risk definition

A customer is **at risk** when two or more of the following are true:

- An overdue invoice exists
- Three or more SLA breaches recorded
- Three or more open support tickets
- Fewer than five average active users over the last 30 days
- Health score below 50

Two or more signals, not one. A single signal is common and not predictive on
its own; the combination is what matters.

## 4. Deliberate exclusions

4.1 Customers in their first 30 days are excluded. Their usage has not ramped
and the score would be misleading.

4.2 Already-churned customers are excluded.

4.3 Service accounts and API-only integrations do not register as active users,
so genuinely API-heavy customers can appear to have low adoption. Check the API
call volume before acting on a low usage score.

## 5. Known limitations

5.1 The score is descriptive, not predictive. It reports the current state; it
does not forecast churn probability from a trained model.

5.2 It weights all products equally, so a customer heavily using one product
and ignoring another scores the same as one using both moderately.

5.3 It does not include sentiment, executive-sponsor changes, or competitive
displacement signals, all of which precede churn and none of which are captured
in the product data.
