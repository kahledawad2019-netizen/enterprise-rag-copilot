---
doc_id: DOC-INC-001
title: Incident Response Procedure
doc_type: incident_procedure
version: "2.0"
effective_date: 2025-02-01
status: current
authority: standard
department: Engineering
owner: VP Engineering
access_group: internal
tenant: all
related_docs: [DOC-SUP-001, DOC-SEC-001, DOC-SLA-001]
tags: [incident, sev1, postmortem, on-call, response]
---

# Incident Response Procedure

**Northwind Cloud** - Version 2.0 - Effective 1 February 2025

## 1. Severity levels

| Severity | Definition | Response |
|---|---|---|
| **SEV1** | Complete outage, data loss risk, or security breach | Immediate, 24x7, all hands |
| **SEV2** | Major degradation affecting many customers | Immediate during covered hours |
| **SEV3** | Limited impact, workaround available | Next business day |

## 2. Roles

- **Incident Commander** - owns the response and makes the decisions, and is
  deliberately not hands-on
- **Operations Lead** - executes mitigation
- **Communications Lead** - customer and internal updates
- **Scribe** - maintains the timeline for the postmortem

For SEV1 these are four different people. The Incident Commander must never
also be the person debugging.

## 3. Timeline commitments

3.1 Detection to declaration: 15 minutes maximum for SEV1.

3.2 Declaration to first customer communication: 30 minutes for SEV1.

3.3 Status page updated every 30 minutes until mitigated.

## 4. Postmortem

4.1 Every SEV1 and SEV2 requires a written postmortem within 5 business days.

4.2 Postmortems are blameless. They describe systems and decisions, never
individuals.

4.3 Every postmortem must contain: timeline, root cause, customer impact, what
went well, what did not go well, and action items with named owners and dates.

4.4 Action items are tracked to completion. Any action item open for more than
60 days is escalated to the VP Engineering.

## 5. Customer credits

5.1 The Incident Commander records measured downtime per affected customer.
Finance determines credit eligibility using the SLA.

5.2 For SEV1 incidents affecting Enterprise customers, credits are applied
proactively without the customer needing to claim them.
