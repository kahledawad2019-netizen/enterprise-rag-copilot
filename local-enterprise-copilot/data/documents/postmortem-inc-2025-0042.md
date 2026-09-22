---
doc_id: DOC-PM-2025-0042
title: "Postmortem INC-2025-0042: Multi-region authentication outage"
doc_type: incident_postmortem
version: "1.0"
effective_date: 2025-06-19
status: current
authority: reference
department: Engineering
owner: Incident Commander
access_group: internal
tenant: all
related_docs: [DOC-INC-001, DOC-SLA-001]
tags: [postmortem, sev1, authentication, certificate, analytics, june 2025, outage, churn]
---

# Postmortem: INC-2025-0042

**Severity:** SEV1 - **Date:** 14 June 2025 - **Duration:** 8 hours 48 minutes

**Products affected:** Northwind Analytics - **Regions:** all

## Summary

On 14 June 2025 an expired intermediate signing certificate caused token
validation to reject every active session for Northwind Analytics across all
regions. Customers could not sign in, and existing sessions failed as their
cached tokens were revalidated.

## Timeline (UTC)

| Time | Event |
|---|---|
| 02:17 | Certificate expires. Token validation begins failing. |
| 02:23 | Automated alerts fire on elevated 401 rates. |
| 02:41 | On-call engineer acknowledges. Incident declared SEV1. |
| 02:55 | First customer communication published to the status page. |
| 04:10 | Root cause identified as the expired intermediate certificate. |
| 05:30 | Replacement certificate issued. Staged rollout begins. |
| 08:45 | Majority of regions recovered. |
| 11:05 | All regions confirmed healthy. Incident resolved. |

## Root cause

The intermediate signing certificate used for session token validation was due
for automated rotation on 7 June 2025. The renewal job failed silently that
week: it exited non-zero, but the alerting rule matched only on job absence,
not on job failure, so no alert fired.

The expired certificate remained cached in memory by running instances, so the
failure did not surface until instances recycled and reloaded it at 02:17 on
14 June, seven days after the renewal should have happened.

## Customer impact

- **99 customers** were affected across all three regions.
- Enterprise customers experienced a full outage for the whole duration.
- Service credits were applied proactively under SLA section 5.
- This incident is the primary driver of the elevated churn observed in the
  second quarter of 2025.

## What went well

- Detection was fast: 6 minutes from first failure to first alert.
- The status page was updated within the 30-minute SEV1 commitment.

## What did not go well

- The renewal job failed for seven days without anyone noticing.
- Alerting checked for job absence rather than job failure.
- There was no independent expiry monitor on the certificate itself.
- Staged rollout took 3 hours 15 minutes and had never been rehearsed at scale.

## Action items

| Action | Owner | Due | Status |
|---|---|---|---|
| Alert on renewal job non-zero exit, not only absence | Platform | 2025-06-30 | Complete |
| Independent certificate-expiry monitor at 30, 14 and 7 days | Platform | 2025-07-15 | Complete |
| Rehearse emergency certificate rollout quarterly | SRE | 2025-09-30 | Complete |
| Cap in-memory certificate caching at 1 hour | Identity | 2025-08-31 | Complete |
