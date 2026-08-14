# Ops runbook — GitOps reconciliation, k3s NetworkPolicy debugging, canary wiring

Hard-won operational knowledge for the k3s/Flux/Istio prod cluster. Written after a
long debugging session (2026-08-03) that chased a canary→Keycloak "connection refused"
down several wrong paths. Read the **TL;DR** boxes first.

---

## 1. Flux: "is it stalled, or just mid-reconcile?"

> **TL;DR:** Committed infra changes under `deploy/` **do auto-apply** — usually within
> **1–3 minutes** of a push. A `flux get kustomizations` snapshot taken *right after a
> push* will show downstream layers as `False` with "dependency '…' is not ready" or
> "revision is not up to date". **That is a normal top-down reconcile wave, not a
> stall.** Wait ~2–3 min and re-check before concluding anything is broken.

### How reconciliation is wired (`deploy/clusters/prod/{infrastructure,apps}.yaml`)

- One `GitRepository` (`flux-system`) tracks `main`, **`interval: 1m`** → a new commit
  is detected within ~1 minute, then drives reconciles via source-change events (the
  per-Kustomization `interval: 10m` is only the *fallback* re-check, not the trigger).
- The Kustomizations form an 8-stage **`dependsOn` graph**, each `wait: true`:
  `namespaces → {cert-manager, gateway-api-crds, cnpg-operator} → istio → opa →
  mesh-policies → identity → gateway → observability`; **apps-prod** depends on
  `mesh-policies` + `identity`.
- `wait: true` means a layer only becomes `Ready` once **all its resources are
  healthy**; the next layer's `dependsOn` gate waits for that. So a push propagates
  **top-down, one healthy layer at a time** — which is exactly why a mid-wave snapshot
  shows lower layers transiently "not ready / not up to date".

### Distinguish a real stall from a wave (do this before alarming anyone)

Compare each Kustomization's **applied revision** to the GitRepository's revision, and
recheck after a couple minutes:

```bash
# what revision has the source fetched?
kubectl -n flux-system get gitrepository flux-system \
  -o jsonpath='{.status.artifact.revision}{"\n"}'          # e.g. main@sha1:41a595ce…

# per-kustomization: Ready + applied revision (want all True at the same sha)
kubectl -n flux-system get kustomizations -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{.status.lastAppliedRevision}{"\n"}{end}'
```

- **All rows `True` at the source sha** → converged; auto-apply worked.
- **A single row genuinely `False` for >10 min while its dependency is `True` at the
  source sha** → a *real* problem in that one layer (look at *its* health checks:
  `kubectl -n flux-system describe kustomization <name>`, and the resources it applies).
  Everything *below* it in the graph will correctly wait — that's the chain doing its
  job, not N separate failures.
- **Controllers themselves:** `kubectl -n flux-system get pods` (source-, kustomize-,
  helm-, notification-controller). A crash-looping `kustomize-controller` stalls
  everything; a healthy one with `RESTARTS` hours/days ago is fine.

### Force an immediate apply (when you can't wait for the wave)

```bash
# re-fetch git NOW, then reconcile a specific layer and everything it gates
flux reconcile source git flux-system
flux reconcile kustomization infra-identity --with-source
```

If the `flux` CLI can't reach the API on a control-plane box (`localhost:8080`
refused), it's using the wrong kubeconfig — run it as `sudo` with the k3s kubeconfig,
or annotate to trigger a reconcile:
`kubectl -n flux-system annotate --overwrite kustomization/<name> reconcile.fluxcd.io/requestedAt="$(date +%s)"`.

**Emergency only** (a canary fix that must land *this second*, wave not yet arrived):
apply the single resource directly with `kubectl patch/apply`. Flux converges it on the
next reconcile since the change is already in git — no drift. Do **not** hand-edit
cluster state that isn't also committed, or Flux will revert it.

---

## 2. Debugging NetworkPolicy connectivity on k3s (kube-router)

> **TL;DR:** k3s ships `ipset`/`iptables` **outside `$PATH`** — a bare `sudo ipset …`
> prints `command not found` and looks like "the set is empty". Always use the bundled
> binary. And a pod's egress can be **allowed** while the connection is still refused —
> because the *destination's ingress* policy rejects it. Localize with **packet
> counters**, not guesswork.

### Use the k3s-bundled tools (this was the single biggest time-sink)

```bash
IPSET=$(find /var/lib/rancher/k3s/data -name ipset -type f | head -1)
sudo "$IPSET" list <SETNAME>                 # members, type/header
sudo "$IPSET" save | grep <ip>               # which sets contain an IP
```

- **`iptables -L -nv` returns nothing** on these nodes (they run **iptables-nft**).
  Use `iptables -S <chain>` or, for **counters**, `iptables-save -c` (each rule is
  prefixed `[pkts:bytes]`).

### kube-router's per-pod model (how a "connection refused" is produced)

Each local pod gets a `KUBE-POD-FW-<hash>` chain. An egress packet runs through the
pod's NetworkPolicy chains (`KUBE-NWPLCY-<hash>`); a matching allow rule
`--match-set KUBE-SRC-… src --match-set KUBE-DST-… dst --dport N -j MARK 0x10000`
sets the accept mark. If nothing marks the packet, the pod-fw chain ends in
`-j REJECT --reject-with icmp-port-unreachable` → the client sees **"connection
refused" in ~1 ms** (a *local* reject; a routing/overlay failure would instead be
"no route to host" or a timeout).

### The decisive test: read the counters

```bash
# does the EGRESS accept rule fire? (src pod → dst :port). Curl the target a few times,
# then diff the [pkts:bytes] on the MARK rule vs the pod-fw REJECT:
sudo iptables-save -c | grep -E 'dport <PORT> -j MARK|REJECT .*POD name:<pod>'
```

- **MARK rule increments, REJECT stays 0** → egress is **allowed**; the refusal is at
  the **far end** — check the *destination's* ingress NetworkPolicy (enforced on the
  destination's node). This is the case that fooled us: the canary's egress to Keycloak
  was fine; Keycloak's *ingress* was rejecting it.
- **MARK rule stays 0, REJECT increments** → egress genuinely not allowed; check the
  source pod's egress policy / the resolved `KUBE-DST-…` set membership.

Red herrings we chased and ruled out: node placement, kube-router "not programming the
peer" (an artifact of `ipset` not-in-PATH), a `k3s-agent` restart, and pod reschedule.
None mattered — the reject followed the pod's **label/identity**, not the node.

---

## 3. Arming a new backend canary — the Keycloak backchannel allowlist

> **TL;DR:** A `<svc>-canary` is a **distinct pod label**, so it is NOT covered by the
> stable service's entries in the identity NetworkPolicies. If the canary does RFC 8693
> token exchange (any backend that reaches other services or Apollo), you **must add its
> label** to `keycloak-ingress` — or every exchange is REJECTed at Keycloak (surfacing
> as a constant **503 / "upstream connect error"**, e.g. when opening the MCP toolset),
> even though the canary's *own* egress policy permits `keycloak:8080`.

When arming `deploy/apps/base/<svc>-canary/*` (see the `canary` skill "ARMED"
prerequisites), also grant the **ingress** carve-outs the canary's distinct label needs:

1. **Keycloak backchannel** — add `<svc>-canary` to the allowlist in
   `deploy/infrastructure/identity/networkpolicies.yaml` → `keycloak-ingress` →
   `ingress[1].from[0].podSelector.matchExpressions[0].values`
   (currently `[search, search-canary, mcp, agents, agents-canary, jobs]`).
   Precedent: `search-canary` and `agents-canary` are already listed.
2. **Its dependency datastores** — any DB/service whose *ingress* selects by the stable
   label must also admit `<svc>-canary`. E.g. `deploy/apps/base/netpol/<svc>-db.yaml`
   ingress had to add `app.kubernetes.io/name: <svc>-canary` alongside `<svc>`
   (the canary shares the stable DB).
3. **Verify** after Flux applies (or `kubectl patch` for immediacy), from the canary
   pod's sidecar:
   ```bash
   kubectl -n prod exec <canary-pod> -c istio-proxy -- \
     curl -sS -m5 -o /dev/null -w '%{http_code}\n' http://<keycloak-pod-ip>:8080/realms/funnelmanager/
   # 200/302 = reachable; 000/exit7 = still blocked
   ```

**Why egress alone isn't enough:** the canary's egress policy (`agents-canary.yaml`)
already allows `identity/keycloak:8080`; NetworkPolicy is enforced on **both** ends, and
Keycloak's namespace has a `default-deny` + an explicit `keycloak-ingress` allowlist. A
new source label is invisible to that allowlist until listed.

---

## See also

- `deploy/clusters/prod/{infrastructure,apps}.yaml` — the Flux dependency graph.
- `deploy/infrastructure/identity/networkpolicies.yaml` — Keycloak ingress/egress.
- `.claude/skills/canary/SKILL.md` — canary lifecycle + ARMED prerequisites.
- `docs/authentication.md` — the RFC 8693 exchange model these policies protect.

---

## Milvus memory model (memory-capped, 2026-08-14) — and what to do when it alerts

Since v1.16.0 Milvus runs a **memory-capped** design: resident memory tracks
*active pages*, not corpus size. The pieces (all in
`deploy/apps/base/data/milvus/`):

- **Global mmap** (`milvus-config` ConfigMap → `user.yaml`): sealed data, indexes,
  and growing segments load via the page cache. Corpus growth lands on **disk**
  (worker2 local-path + MinIO), not RAM (~84GB disk for a 1,009-domain run).
- **`queryNode.cache.readAheadPolicy: random`** — the load-bearing line. The
  default `willneed` pre-faults whole mmap'd files into the page cache, and
  Milvus's own `GetUsedMemoryCount()` counts that cache (cgroup usage), so load
  admission deadlocks at the watermark ("OOM if load" storms) while the real
  working set is far smaller. `random` keeps the cache demand-driven.
- **Write protection** (`growingSegmentsSizeProtection` + default memProtection):
  under memory pressure Milvus throttles/denies inserts instead of dying; leads
  (since v1.16.0) treats a deny as retry-with-backoff inside a per-stream 300s
  budget, and its bounded ingest→embed queue pauses the Apollo walk (`throttled`
  UI events) until embedding catches up.
- **Limit 8Gi = request** (Guaranteed for memory; watermarks engage ~7-7.8GB).
  A ConfigMap-only change **never restarts the pod** — roll it deliberately
  (`kubectl -n prod delete pod milvus-0`) and only when leads is idle (no
  embedding in flight — check `logs deploy/leads -c leads | grep openai`).

**`MilvusMemoryNearLimit` fires** (working set >92% of limit for 15m):
1. `kubectl logs milvus-0 -n prod | grep -cE "OOM if load|no sufficient"` — a
   storm means load admission is wedged; check internal accounting vs working set
   (`grep -oE "memUsage\(MB\)=[0-9.]+"`). Internal ≪ working-set ⇒ page-cache
   inflation (readahead regression?); internal ≈ limit ⇒ the corpus/active-set
   genuinely outgrew the cap.
2. Check write denials (`grep -iE "deny to write|memory quota"`) — leads should
   be backing off, searches keep working.
3. Remedies in order: confirm mmap/readahead config still mounted in the pod;
   prune/compact the collection; only then raise the limit (and the quota) —
   that is the *last* lever, not the first.

**`NodeRootDiskHigh` fires:** disk is the resource that scales with corpus now.
Check `du` of `/var/lib/rancher/k3s/storage/` (milvus mmap + local PVCs) and
MinIO/Mongo growth; prune retired collections/old segments or expand the node
before 90%.

**Alert delivery caveat:** alertmanager is *disabled* — these rules fire in
Prometheus/Grafana (`ALERTS{alertstate="firing"}`) but page nobody. Wiring a
contact point is an open follow-up.
