---
doc_id: DOC-SLA-001
title: Service Level Agreement
doc_type: sla_policy
version: "2.0"
effective_date: 2025-01-01
status: current
authority: policy
department: Support Operations
owner: VP Customer Support
access_group: public
tenant: all
supersedes: DOC-SLA-000
related_docs: [DOC-SUP-001, DOC-REF-001, DOC-INC-001]
tags: [sla, support, response time, uptime, credits, enterprise]
---

# Service Level Agreement

**Northwind Cloud** · Version 2.0 · Effective 1 January 2025

Supersedes version 1.0 (DOC-SLA-000). Tickets opened before 1 January 2025 are
assessed against the version that was in force on the date the ticket was
opened, not against this version.

## 1. Priority definitions

| Priority | Definition |
|---|---|
| **P1** | Complete loss of service, or a security incident affecting production |
| **P2** | Major functionality degraded; no workaround available |
| **P3** | Minor functionality affected, or a workaround exists |
| **P4** | Question, documentation issue, or cosmetic defect |

## 2. First-response targets

The first-response clock starts when the ticket is received and stops when a
**human support engineer** responds. Automated acknowledgements do not stop the
clock.

| Priority | Enterprise | Professional | Starter |
|---|---|---|---|
| **P1** | **15 minutes** | 45 minutes | 4 hours |
| **P2** | 45 minutes | 2 hours | 8 hours |
| **P3** | 3 hours | 8 hours | 16 hours |
| **P4** | 8 hours | 16 hours | 24 hours |

## 3. Resolution targets

| Priority | Enterprise | Professional | Starter |
|---|---|---|---|
| **P1** | 3 hours | 6 hours | 24 hours |
| **P2** | 6 hours | 12 hours | 48 hours |
| **P3** | 36 hours | 72 hours | 120 hours |
| **P4** | 120 hours | 168 hours | 240 hours |

## 4. Coverage and uptime

4.1 Enterprise P1 and P2 are covered 24x7. All other priorities and tiers are
covered during local business hours, Monday to Friday, excluding public
holidays.

4.2 Uptime commitments, measured monthly:

| Tier | Uptime target |
|---|---|
| Enterprise | 99.95 percent |
| Professional | 99.50 percent |
| Starter | 99.00 percent |

4.3 Scheduled maintenance announced at least 72 hours in advance is excluded
from the uptime calculation.

## 5. Service credits

5.1 Where monthly uptime falls below the committed target, the customer may
claim a service credit:

| Measured uptime | Credit (percent of monthly fee) |
|---|---|
| Below target but at or above 99.00 percent | 10 percent |
| 95.00 to 98.99 percent | 25 percent |
| Below 95.00 percent | 50 percent |

5.2 Credits must be claimed within 30 days of the end of the affected month.

5.3 Credits are applied to a future invoice and are never paid in cash. See the
Refund and Credit Policy (DOC-REF-001) section 4.

## 6. Exclusions

The following do not count towards SLA measurement:

- Time spent awaiting information from the customer
- Issues caused by customer configuration or third-party integrations
- Scheduled maintenance announced in accordance with clause 4.3
- Force majeure events
