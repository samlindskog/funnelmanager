# TODO: migrate k3s datastore SQLite → embedded etcd

**Status:** deferred / not urgent. **Priority:** low (a daily size-conditional
compaction timer already prevents the failure that motivated this — see below).
Best done in a planned maintenance window, ideally **paired with going HA**.

## Why
2026-07-30: the k3s SQLite/kine datastore bloated to **3.6 GB / 1.94M revisions**
from leader-election lease-renewal churn (CNPG, Flux×4, Istio…, renewing every
~2s). Compaction stalled once the DB got slow (apiserver `Handler timeout` death
spiral) → `k3s-server` pegged a core, load 9 on 2 cores, Flux health-checks
timing out.

**Mitigated (done):** compacted (kept latest per key) + `VACUUM` → 3.6 GB → 55 MB,
load 9→0.9. Installed `k3s-datastore-compact.timer` on `cp` (usfr4): daily, only
acts if `state.db` > 800 MB, backs up first, keeps 3 backups, logs to
`/var/log/k3s-datastore-compact.log`. This makes recurrence structurally
impossible, so etcd is a "do it right," not an emergency.

**Why etcd is the real fix:** auto-compaction + defrag (no bloat, no timer),
handles control-plane churn properly, and is the foundation for HA.

## The constraint
k3s **cannot convert SQLite → etcd in place** — the datastore backend is fixed at
first `k3s server` start. So this is a **datastore rebuild**: wipe the k8s object
store, reinstall with `--cluster-init`, let GitOps rebuild.

## What migrates automatically vs. what's real work
- **Automatic:** every k8s object is in git → Flux reconciles onto a fresh
  datastore in ~30 min. (etcd stores k8s API objects, NOT app data — Postgres/
  Mongo/Milvus data lives in PVs on the workers, untouched by the swap.)
- **The only manual part = reconnecting stateful pods to their existing on-disk
  data**, because the PVC↔PV binding is the one thing that lives in the datastore
  you're wiping (`local-path`, `Delete` reclaim, data on worker1).

## The ~1–1.5 hr path (realistic, not "half a day")
1. **Pre-flight (no downtime):** `kubectl get secret -A -o yaml` → offline vault
   (bootstrap secrets — keycloak-admin/realm, objectstore-backups, apollo, openai,
   cloudflare, grafana — are NOT in git). Trigger fresh CNPG `Backup` + mongodump.
   Record current k3s install flags. Keep the SQLite `state.db` backup = rollback.
2. `flux suspend` all; maintenance page.
3. Reinstall k3s on `cp` with `--cluster-init` (embedded etcd) + existing flags;
   re-join `edge`/`worker1`(/`worker2`) agents with the new token.
4. Re-apply bootstrap secrets → `flux resume` → infra chain then apps reconcile.
5. **Re-attach data:**
   - Mongo / Milvus / Minio → **static PV manifests** pointing at the existing
     `local-path` hostPath dirs (fast, no data movement).
   - CNPG Postgres ×5 → **restore from S3 barman backup** (`bootstrap.recovery`,
     `s3://funnelmanager-1`). CNPG regenerates creds/certs on a fresh cluster, so
     it can't adopt the old PGDATA — this is the one piece that must restore, not
     re-bind. ~15–20 min for the five small DBs.
6. `prod-health check`, exercise the app, lift maintenance.
7. Optionally remove the compaction timer (etcd auto-compacts; harmless if left).

## Rollback
Reinstall k3s **without** `--cluster-init` (SQLite mode) + drop the backed-up
`state.db` in place → exactly the current state. Keep until etcd is verified.

## Decisions to make
- **HA now or later?** Single-member etcd (just `cp`) = like-for-like, gets the
  auto-compaction/defrag win. **3-member etcd** (promote `edge`/`worker1`[/`worker2`]
  to servers) is the real resilience payoff and the best justification for the
  disruption. Recommend pairing the migration with the HA jump.
- Maintenance window timing; etcd needs more RAM/fsync headroom than SQLite
  (watch `cp` memory on single-member).

## Effort / risk
~1–1.5 hr downtime; moderate risk, well-mitigated by external CNPG backups + the
SQLite rollback anchor. Riskiest bits (test ahead): bootstrap-secret recreation,
Mongo/Milvus/Minio re-bind, CNPG restore.

_When ready, ask to expand this into a full runbook with the exact `--cluster-init`
install, static-PV manifests, and CNPG `bootstrap.recovery` specs (dry-run on a
spare node first)._
