---
name: bad-deploy-rollback
description: Runbook for rolling back a deployment that broke production.
---

# Rollback runbook
Open an incident first so on-call is paged. Check deployment history to find the last
good version, roll back, and confirm error rates recover before closing.
