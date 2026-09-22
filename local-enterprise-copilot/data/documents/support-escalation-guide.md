---
doc_id: DOC-SUP-001
title: Support Escalation Guide
doc_type: support_escalation
version: "2.3"
effective_date: 2025-09-01
status: current
authority: standard
department: Support Operations
owner: Director of Support
access_group: support
tenant: all
related_docs: [DOC-SLA-001, DOC-INC-001]
tags: [support, escalation, tiers, on-call, p1]
---

# Support Escalation Guide

**Northwind Cloud** · Version 2.3 · Effective 1 September 2025

Access group: **support**. Internal runbook for support engineers.

## 1. Support tiers

| Tier | Scope |
|---|---|
| Tier 1 | Triage, known issues, account and billing questions |
| Tier 2 | Product configuration, integration debugging, data investigation |
| Tier 3 | Engineering escalation, code-level defects |
| On-call | Production incidents, 24x7 rotation |

## 2. When to escalate

2.1 Escalate to Tier 2 when the issue is not resolved within 50 percent of the
resolution target, or immediately when it requires log analysis.

2.2 Escalate to Tier 3 when a defect is suspected, or when Tier 2 has not
identified the cause within 4 hours for P1 or 1 business day for P2.

2.3 **Any P1 must be acknowledged by an on-call engineer within 10 minutes**,
which is deliberately tighter than the 15-minute Enterprise first-response
commitment so the SLA is met with margin.

## 3. Escalating to an incident

3.1 Raise a formal incident when two or more customers report the same
symptom, when a core service is unavailable, or when data integrity is in
question.

3.2 Once declared, the Incident Response Procedure (DOC-INC-001) takes over and
the ticket is linked to the incident record.

## 4. Communication

4.1 P1: customer update every 30 minutes until resolved.
4.2 P2: update every 4 hours during covered hours.
4.3 P3 and P4: update on each working day where progress is made.

4.4 Never promise a fix date that engineering has not confirmed.

## 5. Service credit requests

5.1 Support engineers do not approve credits. Route all credit requests to
Finance with the incident reference and measured downtime.

5.2 Eligibility is determined by the SLA (DOC-SLA-001) section 5, not by the
support engineer's judgement.
