---
doc_id: DOC-PM-2025-0031
title: "Postmortem INC-2025-0031: EMEA ingestion latency"
doc_type: incident_postmortem
version: "1.0"
effective_date: 2025-04-11
status: current
authority: reference
department: Engineering
owner: Incident Commander
access_group: internal
tenant: NWC-EU
related_docs: [DOC-INC-001]
tags: [postmortem, sev2, pipeline, emea, autoscaling, latency]
---

# Postmortem: INC-2025-0031

**Severity:** SEV2 - **Date:** 8 April 2025 - **Duration:** 6 hours 50 minutes

**Products affected:** Northwind DataPipeline - **Regions:** EMEA only

## Summary

Ingestion latency in the EMEA region rose from a typical 40 seconds to over 25
minutes during a traffic surge. The ingestion fleet failed to scale out because
an autoscaling threshold had been misconfigured during a capacity review.

## Timeline (UTC)

| Time | Event |
|---|---|
| 09:30 | Traffic surge begins. Queue depth climbs. |
| 09:52 | Latency alert fires. Incident declared SEV2. |
| 10:40 | Autoscaling confirmed not triggering despite queue depth. |
| 11:15 | Threshold misconfiguration identified. |
| 11:50 | Corrected threshold applied. Fleet scales out. |
| 16:20 | Backlog fully drained. Incident resolved. |

## Root cause

A capacity review on 2 April changed the EMEA autoscaling trigger from queue
depth to average CPU. Ingestion workers are I/O bound, so CPU stayed near 35
percent while the queue grew without limit, and the scale-out never fired.

The change was applied to EMEA only, as a pilot. No other region was affected,
which is why the impact is scoped to a single region.

## Customer impact

Customers saw delayed data in dashboards. No data was lost; every record was
processed once the backlog drained. Because this was a latency degradation
rather than an outage, most affected customers were recorded as degraded rather
than as a full outage.

## Action items

| Action | Owner | Due | Status |
|---|---|---|---|
| Revert EMEA autoscaling to a queue-depth trigger | Platform | 2025-04-09 | Complete |
| Require a load test before any autoscaling change | SRE | 2025-05-15 | Complete |
| Alert on queue depth independently of the scaling trigger | Platform | 2025-05-30 | Complete |
