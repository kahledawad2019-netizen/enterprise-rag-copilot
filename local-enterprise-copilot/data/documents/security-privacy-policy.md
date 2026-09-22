---
doc_id: DOC-SEC-001
title: Security and Privacy Policy
doc_type: security_policy
version: "3.1"
effective_date: 2025-11-01
status: current
authority: policy
department: Security
owner: CISO
access_group: security
tenant: all
related_docs: [DOC-INC-001, DOC-CON-001]
tags: [security, privacy, encryption, gdpr, data residency, access]
---

# Security and Privacy Policy

**Northwind Cloud** · Version 3.1 · Effective 1 November 2025

Access group: **security**.

## 1. Data classification

| Class | Examples | Handling |
|---|---|---|
| Public | Marketing material, product catalog | No restriction |
| Internal | Runbooks, architecture notes | Employees only |
| Confidential | Customer data, contracts, pricing | Need-to-know, encrypted at rest |
| Restricted | Credentials, keys, audit logs | Break-glass access, logged |

## 2. Encryption

2.1 All data is encrypted in transit using TLS 1.2 or above. TLS 1.0 and 1.1
are disabled.

2.2 Data at rest is encrypted with AES-256. Key rotation is annual, or
immediately on suspected compromise.

2.3 Customer-managed keys are available on Enterprise plans for Shield and
Analytics.

## 3. Access control

3.1 Access follows least privilege. Production access requires a named
business reason and is time-bound.

3.2 Multi-factor authentication is mandatory for all employee accounts without
exception.

3.3 Production database access by engineers is read-only by default. Write
access requires a change record and expires after 8 hours.

## 4. Data residency

4.1 EMEA customer data is stored in EU regions and does not leave them.

4.2 APAC customers may elect Singapore or Sydney residency.

4.3 Support engineers outside the region may view metadata but not customer
record contents without an explicit, logged customer authorisation.

## 5. Retention and deletion

5.1 Customer data is retained for the contract term plus 90 days.

5.2 On written request, deletion is completed within 30 days and certified.

5.3 Backups are purged on a 35-day rolling cycle; deletion requests are honoured
in backups at the end of that cycle.

## 6. Breach notification

6.1 Confirmed breaches affecting customer data are notified within 72 hours of
confirmation, in line with GDPR Article 33.

6.2 Notification includes the nature of the breach, the data categories
affected, and the mitigation taken.
