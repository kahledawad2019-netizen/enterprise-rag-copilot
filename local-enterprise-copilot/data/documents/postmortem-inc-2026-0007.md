---
doc_id: DOC-PM-2026-0007
title: "Postmortem INC-2026-0007: APAC reporting degradation"
doc_type: incident_postmortem
version: "1.0"
effective_date: 2026-02-23
status: current
authority: reference
department: Engineering
owner: Incident Commander
access_group: internal
tenant: NWC-APAC
related_docs: [DOC-INC-001]
tags: [postmortem, sev2, analytics, apac, query plan, timeout]
---

# Postmortem: INC-2026-0007

**Severity:** SEV2 - **Date:** 19 February 2026 - **Duration:** 4 hours 35 minutes

**Products affected:** Northwind Analytics - **Regions:** APAC only

## Summary

Large tenants in the APAC region experienced report timeouts after an automatic
statistics update caused a query plan regression on the reporting store.

## Timeline (UTC)

| Time | Event |
|---|---|
| 05:10 | Statistics auto-update completes. Plan regression begins. |
| 05:22 | Report timeout alerts fire. Incident declared SEV2. |
| 06:40 | Regression traced to a changed join order on the largest tenant tables. |
| 07:55 | Plan guide applied to force the previous join order. |
| 09:45 | Report latency normal for 90 minutes. Incident resolved. |

## Root cause

An automatic statistics update changed the cardinality estimate for the
reporting fact table. The optimiser switched from a hash join to a nested-loop
join, which is efficient for small tenants and catastrophic for the largest
APAC tenants, where it produced a sixty-fold increase in logical reads.

Smaller tenants were unaffected, which is why most customers were recorded as
degraded or minimal rather than a full outage.

## Action items

| Action | Owner | Due | Status |
|---|---|---|---|
| Pin the plan for the top 10 reporting queries | Data Platform | 2026-03-10 | Complete |
| Alert on p95 report latency per region, not globally | Observability | 2026-03-20 | Complete |
| Review the statistics update schedule for large tenants | Data Platform | 2026-04-15 | Open |
