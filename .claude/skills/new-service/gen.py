#!/usr/bin/env python3
"""new-service — scaffold a new Funnel Manager backend from ONE spec.

A service is a UNION of ingress surfaces, not a binary. Given a small spec, this
driver EMITS every touchpoint category so the service is born lockstep-clean
(grants.py <-> data.json <-> realm proven equal by fm_runtime.export --check;
manifests render; compose parses).

Spec shape (JSON/YAML file via --spec, or flags)::

    {
      "name": "world",          # short name — dir = compose = DNS = image = /api prefix
      "port": 8008,             # container/DNS port (keep unique)
      "stateful": true,         # owns a dedicated <name>-db (CNPG in k3s, postgres in compose)
      "browser": true,          # reached from the ingress gateway at /api/<name>/*
      "ui": false,              # serves an SPA at /<name>/ (a <name>ui container — separate scaffold)
      "callers": [              # INTERNAL callers that exchange toward this service
        {"from": "jobs", "path_prefix": "/internal/jobs"}
      ],
      "deps": []                # services THIS one calls (adds exchange edges + grants + egress)
    }

Every leg is either a NEW file (string template + %%placeholder%% substitution)
or a precise anchored edit into an existing file. JSON policy/realm edits are
minimal (append-only) so the git diff stays reviewable.

The driver FINISHES by running the three gates (fm_runtime.export --check for
both realms, kubectl kustomize, opa test) plus compose config parse; pass
--no-verify to skip.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Repo root: .claude/skills/new-service/gen.py -> parents[3].
ROOT = Path(__file__).resolve().parents[3]

# Container ports of the services a new one may declare as a dependency (for the
# netpol egress rule). Extend when a new dependency target isn't listed.
PORT_MAP = {"search": 8000, "leads": 8001, "mcp": 8003, "mail": 8004, "jobs": 8005, "agents": 8006}

# Backend prod resource profile (CONVENTIONS.md; the jobs/search baseline).
RES = {"cpu_req": "50m", "mem_req": "128Mi", "cpu_lim": "500m", "mem_lim": "512Mi", "compose_mem": "512m"}

created: list[str] = []
modified: set[str] = set()


# --------------------------------------------------------------------------
# low-level file helpers
# --------------------------------------------------------------------------
def _p(rel: str) -> Path:
    return ROOT / rel


def sub(template: str, ctx: dict) -> str:
    out = template
    for k, v in ctx.items():
        out = out.replace(f"%%{k}%%", str(v))
    return out


def emit(rel: str, content: str) -> None:
    path = _p(rel)
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing file: {rel}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    created.append(rel)


def edit(rel: str, old: str, new: str, *, count: int = 1, required: bool = True) -> None:
    path = _p(rel)
    text = path.read_text()
    if old not in text:
        if required:
            raise SystemExit(f"anchor not found in {rel}:\n---\n{old}\n---")
        print(f"  ! soft-skip (anchor absent) in {rel}", file=sys.stderr)
        return
    path.write_text(text.replace(old, new, count if count > 0 else -1))
    modified.add(rel)


def edit_re(rel: str, pattern: str, repl: str, *, count: int = 0, required: bool = True) -> None:
    path = _p(rel)
    text = path.read_text()
    new_text, n = re.subn(pattern, repl, text, count=count)
    if n == 0:
        if required:
            raise SystemExit(f"regex {pattern!r} matched nothing in {rel}")
        print(f"  ! soft-skip (regex absent) in {rel}", file=sys.stderr)
        return
    path.write_text(new_text)
    modified.add(rel)


def insert_after(rel: str, anchor: str, insertion: str, *, required: bool = True) -> None:
    edit(rel, anchor, anchor + insertion, required=required)


def insert_before(rel: str, anchor: str, insertion: str, *, required: bool = True) -> None:
    edit(rel, anchor, insertion + anchor, required=required)


def edit_json(rel: str, mutate) -> None:
    path = _p(rel)
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    modified.add(rel)


# ==========================================================================
# NEW-FILE templates
# ==========================================================================
TPL_CONFIG = '''\
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Funnel Manager %%Name%%"
%%CONFIG_FIELDS%%

@lru_cache
def get_settings() -> Settings:
    return Settings()
'''

TPL_DATABASE = '''\
"""Dedicated %%DB_NAME%% store. The engine is lazy (no connection at import), so
``import app.main`` succeeds without a database — init_db runs in the lifespan."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings


def _make_engine() -> AsyncEngine:
    url = get_settings().database_url
    # CNPG's generated secret has a plain postgresql:// URI — force asyncpg.
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return create_async_engine(url, pool_pre_ping=True)


engine = _make_engine()
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    # Register ORM metadata + create_all here once models exist. The scaffold
    # only proves connectivity so a fresh service starts clean.
    async with engine.begin():
        pass
'''

TPL_DOCKERFILE = '''\
# Build context: repository root (needed for libs/fm_runtime).
FROM python:3.13-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY libs/fm_runtime ./libs/fm_runtime
COPY %%name%%/requirements.txt ./requirements.txt
RUN pip install --prefix=/install ./libs/fm_runtime -r requirements.txt

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --user-group --no-create-home app
COPY --from=build /install /usr/local
WORKDIR /srv
COPY %%name%%/app ./app
USER 10001
EXPOSE %%port%%
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "%%port%%"]
'''

TPL_DOCKERFILE_DEV = '''\
# Build context: repository root. Source is bind-mounted (./%%name%% -> /app,
# ./libs/fm_runtime -> /libs/fm_runtime for live lib edits via -e install).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY libs/fm_runtime /libs/fm_runtime
COPY %%name%%/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -e /libs/fm_runtime -r /tmp/requirements.txt
EXPOSE %%port%%
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "%%port%%", "--reload"]
'''

TPL_DOCKERIGNORE = ".venv/\nvenv/\n__pycache__/\n*.py[cod]\n*.db\n.env\n.pytest_cache/\nscripts/\n"

TPL_ENV_EXAMPLE = '''\
# %%Name%% backend — local non-Docker run (copy to %%name%%/.env)

%%ENV_STORAGE%%# --- Identity / authz (fm_runtime FM_* vars) -------------------------------
# This service's audience is `%%name%%`. Internal calls EXCHANGE, never forward.
FM_SERVICE_NAME=%%name%%
# FM_OIDC_ISSUER=http://localhost:8080/realms/funnelmanager
# FM_OIDC_CLIENT_ID=%%name%%
# FM_OIDC_CLIENT_SECRET=dev-%%name%%-secret
# FM_JWT_VERIFY=true          # verify JWTs locally outside the mesh
# FM_ENFORCE_GRANTS=true      # apply role grants in-process (compose)
'''

TPL_REQS_STATEFUL = "fastapi==0.115.12\nuvicorn[standard]==0.34.2\nhttpx==0.28.1\npydantic-settings==2.9.1\nsqlalchemy==2.0.40\nasyncpg==0.30.0\ngreenlet==3.2.1\n"
TPL_REQS_STATELESS = "fastapi==0.115.12\nuvicorn[standard]==0.34.2\nhttpx==0.28.1\npydantic-settings==2.9.1\n"

TPL_DEPLOYMENT = '''\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: %%name%%
  labels:
    app.kubernetes.io/name: %%name%%
    app.kubernetes.io/part-of: funnelmanager
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: %%name%%
  template:
    metadata:
      labels:
        app.kubernetes.io/name: %%name%%
        app.kubernetes.io/part-of: funnelmanager
      annotations:
        # Istio's metrics merge rewrites these to the sidecar's :15020.
        prometheus.io/scrape: "true"
        prometheus.io/port: "%%port%%"
        prometheus.io/path: /metrics
    spec:
      serviceAccountName: %%name%%
      nodeSelector:
        role: worker
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: %%name%%
          # Tag is pinned only in overlays via the images transformer.
          image: ghcr.io/samlindskog/funnelmanager/%%name%%
          ports:
            - name: http
              containerPort: %%port%%
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          resources:
            requests:
              cpu: %%cpu_req%%
              memory: %%mem_req%%
            limits:
              cpu: %%cpu_lim%%
              memory: %%mem_lim%%
          livenessProbe:
            httpGet:
              path: /healthz
              port: %%port%%
            initialDelaySeconds: 5
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /readyz
              port: %%port%%
            initialDelaySeconds: 5
            periodSeconds: 10
          env:
            - name: FM_SERVICE_NAME
              value: %%name%%
            # The mesh validates JWTs (RequestAuthentication); the app trusts them.
            - name: FM_JWT_VERIFY
              value: "false"
            - name: FM_OIDC_ISSUER
              valueFrom:
                configMapKeyRef:
                  name: fm-oidc
                  key: issuer
            # In-cluster token endpoint (split horizon).
            - name: FM_OIDC_TOKEN_URL
              valueFrom:
                configMapKeyRef:
                  name: fm-oidc
                  key: token-url
            - name: FM_OIDC_CLIENT_ID
              value: %%name%%
            - name: FM_OIDC_CLIENT_SECRET
              valueFrom:
                secretKeyRef:
                  name: fm-oidc-%%name%%
                  key: client-secret
            - name: FM_LOG_LEVEL
              value: INFO
%%K8S_ENV_EXTRA%%'''

TPL_SERVICE = '''\
apiVersion: v1
kind: Service
metadata:
  name: %%name%%
  labels:
    app.kubernetes.io/name: %%name%%
    app.kubernetes.io/part-of: funnelmanager
spec:
  selector:
    app.kubernetes.io/name: %%name%%
  ports:
    - name: http
      port: %%port%%
      targetPort: %%port%%
'''

TPL_SERVICEACCOUNT = '''\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: %%name%%
  labels:
    app.kubernetes.io/name: %%name%%
    app.kubernetes.io/part-of: funnelmanager
'''

TPL_KUSTOMIZATION = '''\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - serviceaccount.yaml
  - deployment.yaml
  - service.yaml
'''

TPL_DB_CLUSTER = '''\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: %%name%%-db
  labels:
    app.kubernetes.io/name: %%name%%-db
    app.kubernetes.io/part-of: funnelmanager
spec:
  instances: 1
  inheritedMetadata:
    labels:
      app.kubernetes.io/name: %%name%%-db
      app.kubernetes.io/part-of: funnelmanager
    annotations:
      sidecar.istio.io/inject: "false"
  bootstrap:
    initdb:
      database: %%DB_NAME%%
      owner: funnel
  storage:
    size: 5Gi
    storageClass: local-path
  resources:
    requests:
      cpu: 100m
      memory: 192Mi
    limits:
      cpu: 500m
      memory: 512Mi
  affinity:
    nodeSelector:
      role: worker
    enablePodAntiAffinity: true
    podAntiAffinityType: preferred
    topologyKey: kubernetes.io/hostname
  backup:
    retentionPolicy: "14d"
    barmanObjectStore:
      destinationPath: s3://funnelmanager-1/%%name%%-db
      endpointURL: https://us-lax-4.linodeobjects.com
      s3Credentials:
        accessKeyId:
          name: objectstore-backups
          key: ACCESS_KEY_ID
        secretAccessKey:
          name: objectstore-backups
          key: ACCESS_SECRET_KEY
'''

TPL_DB_BACKUP = '''\
apiVersion: postgresql.cnpg.io/v1
kind: ScheduledBackup
metadata:
  name: %%name%%-db-daily
  labels:
    app.kubernetes.io/name: %%name%%-db
    app.kubernetes.io/part-of: funnelmanager
spec:
  schedule: "0 45 3 * * *"
  immediate: true
  backupOwnerReference: self
  cluster:
    name: %%name%%-db
'''

TPL_DB_KUSTOMIZATION = '''\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - cluster.yaml
  - scheduledbackup.yaml
'''

TPL_DB_NETPOL = '''\
# %%name%%-db (CNPG Postgres): %%name%% is its only client. NetworkPolicy is
# layer 3 of 3 (OPA pairing %%name%%->%%name%%-db + Istio L4 also apply).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: %%name%%-db
  labels:
    app.kubernetes.io/name: %%name%%-db
    app.kubernetes.io/part-of: funnelmanager
spec:
  podSelector:
    matchLabels:
      cnpg.io/cluster: %%name%%-db
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: %%name%%
      ports:
        - protocol: TCP
          port: 5432
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: cnpg-system
  egress:
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
      ports:
        - protocol: TCP
          port: 443
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
      ports:
        - protocol: TCP
          port: 6443
'''

TPL_GW_CANARY = '''\
# ARMED-IDLE gateway canary route for %%name%% (scaffolded by new-service) —
# "canary-if-exists-else-stable". A SEPARATE HTTPRoute for the cookie-marked
# canary path: it PRESENCE-matches `x-fm-canary` (RegularExpression `.+`, NEVER
# the secret) — the canary-cookie-gate EnvoyFilter makes the marker non-forgeable
# by stripping any client-supplied header and re-injecting the secret (from the
# `fm-canary-token` k8s Secret, read via os.getenv) only for a valid host-only
# `fm_canary` cookie, so presence is sufficient and NO secret lives in this file.
# It is listed in gateway/kustomization.yaml ONLY while `%%name%%-canary` is
# scaled up (route presence == canary activation), so an idle canary never 503s
# the marker — it falls through to the stable /api/%%name%%/ rule. The canary
# tooling (build-canary.yml + canary.sh) owns that toggle.
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: %%name%%-canary
  namespace: istio-ingress
spec:
  parentRefs:
    - name: fm-gateway
      sectionName: https-app
  rules:
    - matches:
        - path: { type: PathPrefix, value: /api/%%name%%/ }
          headers:
            - name: x-fm-canary
              type: RegularExpression
              value: ".+"
      backendRefs:
        - name: %%name%%-canary
          namespace: prod
          port: %%port%%
'''

TPL_EW_CANARY = '''\
# ARMED-IDLE east-west canary VirtualService for the INTERNAL service %%name%%
# (scaffolded by new-service) — the in-mesh analogue of the gateway canary
# HTTPRoutes. It attaches to the SIDECARS (gateways: [mesh], NEVER the
# fm-gateway) for host `%%name%%` and PRESENCE-matches `x-fm-canary` (regex `.+`,
# NEVER the secret value — the marker is already non-forgeable in-mesh). Listed
# in mesh-policies/kustomization.yaml ONLY while `%%name%%-canary` is scaled up
# (VS presence == canary activation); an idle canary has no VS and the marker
# falls through to stable `%%name%%`. The canary tooling owns that toggle.
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: %%name%%-canary-eastwest
  namespace: prod
  labels:
    app.kubernetes.io/name: %%name%%-canary
    app.kubernetes.io/part-of: funnelmanager
spec:
  hosts:
    - %%name%%
  gateways:
    - mesh
  http:
    - name: canary-marked
      match:
        - headers:
            x-fm-canary:
              regex: ".+"
      route:
        - destination:
            host: %%name%%-canary
            port:
              number: %%port%%
    - name: default-stable
      route:
        - destination:
            host: %%name%%
            port:
              number: %%port%%
'''

TPL_AGENT_MD = '''\
---
name: %%name%%-agent
description: Owns the %%name%% backend (%%name%%/) — a FastAPI service on :%%port%%%%DESC_SURFACE%%. NEW service (scaffolded by .claude/skills/new-service).
model: opus
---

You own `%%name%%/` (`:%%port%%`%%DB_NOTE%%), scaffolded by the `new-service` skill.
Read the project `CLAUDE.md` first — this is your delta.

## Your boundary
- Edit only `%%name%%/`. Contract changes to another service are a hand-off to its agent.
- Cross-user visible by design (P1): the `%%name%%-access` role gates whether a principal
  may call the API, never which rows they see. Attribute, don't hide.
- **Mesh-agnostic (P10):** authorization is platform-enforced (OPA / `fm_runtime` grants) —
  never re-implement it. Internal hops EXCHANGE, never forward: use `fm_runtime`'s
  `InternalClient(base_url, audience=...)` at the call site, not inline exchange policy.

## Invariants
- Auth via `fm_runtime` (audience `%%name%%`). Per-hop audience is enforced.%%SURFACE_INV%%
- `GET /api/%%name%%/health` is `@anonymous` — return **liveness only**, never config /
  topology (info-disclosure). The `fm_runtime` `/healthz` `/readyz` already exist.
- Backend imports absolute from `app`, CWD `%%name%%/`.%%DB_INV%%
- Never raise once an NDJSON stream has started (P8): emit `{"type":"error",...}` as a line.

## Verify
Run the flow end-to-end. For any role/scope/anonymous change, the FIRST check is
`python3 -m fm_runtime.export --check deploy/policy/data.json --realm deploy/keycloak/realm-funnelmanager-dev.json`.
Add unit + integration tests at the level your change touches (P11).

## When done
Clean `git diff`, hand off to the adversarial reviewers. **Always** include
`security-reviewer` for any policy / realm / audience-scope / network-exposure change.
'''


# ==========================================================================
# assembly of the parts that toggle on stateful/browser
# ==========================================================================
def build_main(ctx: dict, stateful: bool, browser: bool) -> str:
    L = ["from contextlib import asynccontextmanager", "", "from fastapi import FastAPI"]
    if browser:
        L.append("from fastapi.middleware.cors import CORSMiddleware")
    L.append("from fm_runtime import anonymous, install")
    if stateful:
        L.append("from sqlalchemy import text")
    L += ["", "from app.config import get_settings"]
    if stateful:
        L.append("from app.database import engine, init_db")
    L += ["", ""]
    if stateful:
        L += [
            "@asynccontextmanager",
            "async def lifespan(_: FastAPI):",
            "    await init_db()",
            "    yield",
            "",
            "",
            "async def _db_ready() -> None:",
            "    async with engine.connect() as conn:",
            '        await conn.execute(text("SELECT 1"))',
            "",
            "",
            "settings = get_settings()",
            "app = FastAPI(title=settings.app_name, lifespan=lifespan)",
            "",
            "# fm_runtime installs PrincipalMiddleware (audience `%%name%%`), structured",
            "# logging, /healthz, /readyz, /metrics, and /api/%%name%%/whoami.",
            'install(app, service="%%name%%", ready_checks={"postgres": _db_ready})',
        ]
    else:
        L += [
            "settings = get_settings()",
            "app = FastAPI(title=settings.app_name)",
            "",
            "# fm_runtime installs PrincipalMiddleware (audience `%%name%%`), structured",
            "# logging, /healthz, /readyz, /metrics, and /api/%%name%%/whoami.",
            'install(app, service="%%name%%")',
        ]
    if browser:
        L += [
            "",
            "app.add_middleware(",
            "    CORSMiddleware,",
            "    allow_origins=settings.cors_origin_list,",
            "    allow_credentials=True,",
            '    allow_methods=["*"],',
            '    allow_headers=["*"],',
            ")",
        ]
    L += [
        "",
        "# Mount your routers under /api/%%name%% here, e.g.:",
        "#   from app.routers import tasks",
        "#   app.include_router(tasks.router)",
        "",
        "",
        '@app.get("/api/%%name%%/health")',
        '@anonymous("legacy health path (compose-era); k8s probes use /healthz + /readyz")',
        "async def health() -> dict[str, str]:",
        '    return {"status": "ok"}',
        "",
    ]
    return sub("\n".join(L), ctx)


def build_config(ctx: dict, stateful: bool, browser: bool) -> str:
    fields = []
    if stateful:
        fields.append('    database_url: str = "postgresql+asyncpg://funnel:funnel@%%name%%-db:5432/%%DB_NAME%%"')
    if browser:
        fields.append('    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"')
        fields.append("")
        fields.append("    @property")
        fields.append("    def cors_origin_list(self) -> list[str]:")
        fields.append('        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]')
    if not fields:
        fields.append("    # (no service-specific settings yet)")
    return sub(TPL_CONFIG, dict(ctx, CONFIG_FIELDS="\n".join(fields) + "\n"))


def build_k8s_env_extra(ctx: dict, stateful: bool, browser: bool) -> str:
    parts = []
    if stateful:
        parts.append(
            "            - name: DATABASE_URL\n"
            "              valueFrom:\n"
            "                secretKeyRef:\n"
            "                  name: %%name%%-db-app\n"
            "                  key: uri\n"
        )
    if browser:
        parts.append(
            "            - name: CORS_ORIGINS\n"
            "              valueFrom:\n"
            "                configMapKeyRef:\n"
            "                  name: fm-web\n"
            "                  key: cors-origins\n"
        )
    return sub("".join(parts), ctx)


def build_netpol(spec: dict, stateful: bool, browser: bool) -> str:
    name, port = spec["name"], spec["port"]
    ingress = []
    if browser:
        ingress.append(
            "    - from:\n        - namespaceSelector:\n            matchLabels:\n"
            "              kubernetes.io/metadata.name: istio-ingress\n"
            f"      ports:\n        - protocol: TCP\n          port: {port}\n"
        )
    for c in spec["callers"]:
        ingress.append(
            f"    # {c['from']} exchanges toward {name}; OPA additionally gates the hop.\n"
            "    - from:\n        - podSelector:\n            matchLabels:\n"
            f"              app.kubernetes.io/name: {c['from']}\n"
            f"      ports:\n        - protocol: TCP\n          port: {port}\n"
        )
    egress = []
    if stateful:
        egress.append(
            "    # Dedicated Postgres (CNPG labels instance pods cnpg.io/cluster).\n"
            "    - to:\n        - podSelector:\n            matchLabels:\n"
            f"              cnpg.io/cluster: {name}-db\n"
            "      ports:\n        - protocol: TCP\n          port: 5432\n"
        )
    for d in spec["deps"]:
        egress.append(
            f"    - to:\n        - podSelector:\n            matchLabels:\n"
            f"              app.kubernetes.io/name: {d}\n"
            f"      ports:\n        - protocol: TCP\n          port: {PORT_MAP[d]}\n"
        )
    egress.append(
        "    # Keycloak (in-cluster token endpoint for RFC 8693 exchange).\n"
        "    - to:\n        - namespaceSelector:\n            matchLabels:\n"
        "              kubernetes.io/metadata.name: identity\n"
        "          podSelector:\n            matchLabels:\n"
        "              app.kubernetes.io/name: keycloak\n"
        "      ports:\n        - protocol: TCP\n          port: 8080\n"
    )
    return (
        f"# {name}: NetworkPolicy (layer 3 of 3 with OPA + Istio L4). Scaffolded by\n"
        "# new-service — least-privilege ingress/egress derived from the spec.\n"
        "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n"
        f"  name: {name}\n  labels:\n    app.kubernetes.io/name: {name}\n"
        "    app.kubernetes.io/part-of: funnelmanager\nspec:\n"
        f"  podSelector:\n    matchLabels:\n      app.kubernetes.io/name: {name}\n"
        "  policyTypes:\n    - Ingress\n    - Egress\n"
        "  ingress:\n" + "".join(ingress) + "  egress:\n" + "".join(egress)
    )


def build_compose_service(spec: dict, env: str, stateful: bool, browser: bool) -> str:
    name, port = spec["name"], spec["port"]
    up = name.upper()
    L = []
    if stateful:
        pw = "${POSTGRES_PASSWORD:-funnel}" if env == "dev" else "${POSTGRES_PASSWORD}"
        usr = "${POSTGRES_USER:-funnel}" if env == "dev" else "${POSTGRES_USER}"
        vol = f"{name}_postgres_data_dev" if env == "dev" else f"{name}_postgres_data"
        L += [f"  # Dedicated Postgres for the {name} store (own db funnelmanager_{name}; mail-db pattern).",
              f"  {name}-db:", "    image: postgres:16-alpine"]
        if env == "prod":
            L.append("    restart: unless-stopped")
        L += ["    mem_limit: 256m", "    environment:",
              f"      POSTGRES_USER: {usr}", f"      POSTGRES_PASSWORD: {pw}",
              f"      POSTGRES_DB: funnelmanager_{name}", "    volumes:",
              f"      - {vol}:/var/lib/postgresql/data", "    healthcheck:",
              f'      test: ["CMD-SHELL", "pg_isready -U {usr} -d funnelmanager_{name}"]',
              "      interval: 5s", "      timeout: 5s", "      retries: 10", ""]
    surface = "browser-facing (nginx/gateway-routed)" if browser else "internal only (loopback publish like mcp)"
    L += [f"  # {name.capitalize()} service (:{port}) — {surface}. Scaffolded by new-service.", f"  {name}:"]
    if env == "dev":
        L += ["    build:", "      context: .", f"      dockerfile: {name}/Dockerfile.dev"]
    else:
        L += [f"    image: ghcr.io/samlindskog/funnelmanager/{name}:${{IMAGE_TAG:-prod}}", "    restart: unless-stopped"]
    L.append(f"    mem_limit: {RES['compose_mem']}")
    if env == "dev":
        L += ["    env_file:", "      - .env"]
    L.append("    environment:")
    if stateful:
        if env == "dev":
            L.append(f"      DATABASE_URL: postgresql+asyncpg://${{POSTGRES_USER:-funnel}}:${{POSTGRES_PASSWORD:-funnel}}@{name}-db:5432/funnelmanager_{name}")
        else:
            L.append(f"      DATABASE_URL: ${{{up}_DATABASE_URL}}")
    L.append(f"      FM_SERVICE_NAME: {name}")
    L.append('      FM_JWT_VERIFY: "true"')
    L.append("      FM_OIDC_ISSUER: ${FM_OIDC_ISSUER:-http://localhost:8080/realms/funnelmanager}" if env == "dev"
             else "      FM_OIDC_ISSUER: ${FM_OIDC_ISSUER}")
    L += ["      FM_OIDC_TOKEN_URL: http://keycloak:8080/realms/funnelmanager/protocol/openid-connect/token",
          "      FM_OIDC_JWKS_URL: http://keycloak:8080/realms/funnelmanager/protocol/openid-connect/certs",
          f"      FM_OIDC_CLIENT_ID: {name}"]
    L.append(f"      FM_OIDC_CLIENT_SECRET: ${{FM_OIDC_{up}_SECRET:-dev-{name}-secret}}" if env == "dev"
             else f"      FM_OIDC_CLIENT_SECRET: ${{FM_OIDC_{up}_SECRET}}")
    L.append('      FM_ENFORCE_GRANTS: "true"')
    if browser:
        L.append("      CORS_ORIGINS: ${CORS_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000,http://localhost}"
                 if env == "dev" else "      CORS_ORIGINS: ${CORS_ORIGINS}")
    if env == "dev":
        L += ["    volumes:", f"      - ./{name}:/app", "      - ./libs/fm_runtime:/libs/fm_runtime"]
    if not browser:  # loopback publish (mcp/jobs pattern)
        L.append("    ports:")
        L.append(f'      - "${{HOST_BIND:-}}{port}:{port}"' if env == "dev"
                 else f'      - "${{HOST_BIND:-127.0.0.1:}}${{PROD_{up}_PORT:-{port}}}:{port}"')
    L += ["    expose:", f'      - "{port}"', "    depends_on:"]
    if stateful:
        L += [f"      {name}-db:", "        condition: service_healthy"]
    L += ["      keycloak:", f"        condition: {'service_healthy' if env == 'dev' else 'service_started'}"]
    return "\n".join(L) + "\n\n"


# ==========================================================================
# spec + generation
# ==========================================================================
def load_spec(args) -> dict:
    if args.spec:
        raw = Path(args.spec).read_text()
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            import yaml  # type: ignore
            spec = yaml.safe_load(raw)
    else:
        if not args.name or not args.port:
            raise SystemExit("provide --spec FILE, or at least --name and --port")
        callers = []
        for c in args.caller or []:
            frm, _, pp = c.partition(":")
            callers.append({"from": frm, "path_prefix": pp or None})
        spec = {"name": args.name, "port": args.port, "stateful": args.stateful,
                "browser": args.browser, "ui": args.ui, "callers": callers, "deps": args.dep or []}
    spec.setdefault("stateful", False)
    spec.setdefault("browser", False)
    spec.setdefault("ui", False)
    spec.setdefault("callers", [])
    spec.setdefault("deps", [])
    spec["callers"] = [{"from": c["from"], "path_prefix": c.get("path_prefix")} for c in spec["callers"]]
    if not re.fullmatch(r"[a-z][a-z0-9]{1,15}", spec["name"]):
        raise SystemExit("name must be lowercase [a-z][a-z0-9]{1,15}")
    # port is substituted RAW (%%port%%) into generated YAML/compose/nginx — a
    # non-int (e.g. a multiline string) could inject extra config keys. Require
    # an int in the unprivileged range.
    try:
        spec["port"] = int(spec["port"])
    except (TypeError, ValueError):
        raise SystemExit("port must be an integer")
    if not (1024 <= spec["port"] <= 65535):
        raise SystemExit("port must be in 1024..65535")
    for d in spec["deps"]:
        if d not in PORT_MAP:
            raise SystemExit(f"unknown dependency {d!r}; add its port to PORT_MAP")
    # callers[].from is spliced RAW into deploy/policy/data.json (callers/azp_allow
    # — the live OPA policy source), libs/fm_runtime/fm_runtime/grants.py, and
    # netpol; callers[].path_prefix into data.json path_prefixes. Validate BOTH
    # against strict allowlists BEFORE any template substitution so a crafted
    # --spec cannot inject policy entries or break out of the JSON/Python
    # structure (fm_runtime.export --check only catches DIVERGENCE, not a
    # mutually-consistent malicious addition). name/deps are already validated.
    for c in spec["callers"]:
        if not re.fullmatch(r"[a-z][a-z0-9]{1,15}", c["from"] or ""):
            raise SystemExit(f"caller 'from' must be a client id [a-z][a-z0-9]{{1,15}}: {c['from']!r}")
        if c.get("path_prefix") is not None and not re.fullmatch(r"/[A-Za-z0-9/_.-]*", c["path_prefix"]):
            raise SystemExit(f"caller path_prefix must be a safe path /[A-Za-z0-9/_.-]*: {c['path_prefix']!r}")
    if spec["ui"]:
        spec["browser"] = True
    return spec


def generate(spec: dict) -> None:
    name, port = spec["name"], spec["port"]
    stateful, browser, ui = bool(spec["stateful"]), bool(spec["browser"]), bool(spec["ui"])
    up = name.upper()
    dev_secret, prod_secret = f"dev-{name}-secret", f"REPLACE-{name}-secret"
    pin = re.search(r"newTag: (sha-[0-9a-f]+)", _p("deploy/apps/overlays/prod/kustomization.yaml").read_text()).group(1)
    # NOTE: no cookie_secret extraction — the canary secret was externalized to
    # the fm-canary-token k8s Secret (commit 09b739bb); canary routes presence-
    # match `x-fm-canary` and carry no secret. See TPL_GW_CANARY / TPL_EW_CANARY.
    caller_clients = [c["from"] for c in spec["callers"]]
    azp = (["frontend"] if browser else []) + caller_clients

    ctx = dict(name=name, Name=name.capitalize(), NAME_UPPER=up, port=port,
               DB_NAME=f"funnelmanager_{name}", pin=pin,
               cpu_req=RES["cpu_req"], mem_req=RES["mem_req"], cpu_lim=RES["cpu_lim"], mem_lim=RES["mem_lim"])

    # ---- 1. source skeleton -------------------------------------------------
    emit(f"{name}/app/__init__.py", "")
    emit(f"{name}/app/main.py", build_main(ctx, stateful, browser))
    emit(f"{name}/app/config.py", build_config(ctx, stateful, browser))
    if stateful:
        emit(f"{name}/app/database.py", sub(TPL_DATABASE, ctx))
    emit(f"{name}/Dockerfile", sub(TPL_DOCKERFILE, ctx))
    emit(f"{name}/Dockerfile.dev", sub(TPL_DOCKERFILE_DEV, ctx))
    emit(f"{name}/requirements.txt", TPL_REQS_STATEFUL if stateful else TPL_REQS_STATELESS)
    emit(f"{name}/.dockerignore", TPL_DOCKERIGNORE)
    env_storage = ""
    if stateful:
        env_storage = ("# --- Storage ---------------------------------------------------------------\n"
                       f"DATABASE_URL=postgresql+asyncpg://funnel:funnel@localhost:5432/funnelmanager_{name}\n\n")
    if browser:
        env_storage += "CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173\n\n"
    emit(f"{name}/.env.example", sub(TPL_ENV_EXAMPLE, dict(ctx, ENV_STORAGE=env_storage)))

    # ---- 2. compose ---------------------------------------------------------
    insert_before("docker-compose.dev.yml", "  nginx:\n    image: nginx:1.27-alpine\n",
                  build_compose_service(spec, "dev", stateful, browser))
    insert_before("docker-compose.prod.yml",
                  "  frontend:\n    image: ghcr.io/samlindskog/funnelmanager/frontend:${IMAGE_TAG:-prod}\n",
                  build_compose_service(spec, "prod", stateful, browser))
    if stateful:
        insert_after("docker-compose.dev.yml", "volumes:\n  postgres_data_dev:\n", f"  {name}_postgres_data_dev:\n")
        insert_after("docker-compose.prod.yml", "  postgres_data:\n", f"  {name}_postgres_data:\n")

    # ---- 3. k3s base app ----------------------------------------------------
    emit(f"deploy/apps/base/{name}/deployment.yaml",
         sub(TPL_DEPLOYMENT, dict(ctx, K8S_ENV_EXTRA=build_k8s_env_extra(ctx, stateful, browser))))
    emit(f"deploy/apps/base/{name}/service.yaml", sub(TPL_SERVICE, ctx))
    emit(f"deploy/apps/base/{name}/serviceaccount.yaml", sub(TPL_SERVICEACCOUNT, ctx))
    emit(f"deploy/apps/base/{name}/kustomization.yaml", TPL_KUSTOMIZATION)

    # ---- 4. k3s data db -----------------------------------------------------
    if stateful:
        emit(f"deploy/apps/base/data/{name}-db/cluster.yaml", sub(TPL_DB_CLUSTER, ctx))
        emit(f"deploy/apps/base/data/{name}-db/scheduledbackup.yaml", sub(TPL_DB_BACKUP, ctx))
        emit(f"deploy/apps/base/data/{name}-db/kustomization.yaml", TPL_DB_KUSTOMIZATION)
        insert_after("deploy/apps/base/data/kustomization.yaml", "  - agents-db\n", f"  - {name}-db\n")

    # ---- 5. netpol ----------------------------------------------------------
    emit(f"deploy/apps/base/netpol/{name}.yaml", build_netpol(spec, stateful, browser))
    insert_after("deploy/apps/base/netpol/kustomization.yaml", "  - agents.yaml\n", f"  - {name}.yaml\n")
    if stateful:
        emit(f"deploy/apps/base/netpol/{name}-db.yaml", sub(TPL_DB_NETPOL, ctx))
        insert_after("deploy/apps/base/netpol/kustomization.yaml", "  - agents-db.yaml\n", f"  - {name}-db.yaml\n")

    # ---- 6. overlay resources + images -------------------------------------
    insert_after("deploy/apps/overlays/prod/kustomization.yaml", "  - ../../base/agents\n", f"  - ../../base/{name}\n")
    insert_after("deploy/apps/overlays/prod/kustomization.yaml",
                 f"  - name: ghcr.io/samlindskog/funnelmanager/backup\n    newTag: {pin}\n",
                 f"  - name: ghcr.io/samlindskog/funnelmanager/{name}\n    newTag: {pin}\n")

    # ---- 7. realm (dev + prod example) -------------------------------------
    def mutate_realm(secret):
        def _m(realm):
            realm["clientScopes"].append({
                "name": f"svc-{name}", "protocol": "openid-connect",
                "description": f"Adds the {name} service to the token audience (used by RFC 8693 exchange)",
                "attributes": {"include.in.token.scope": "false"},
                "protocolMappers": [{
                    "name": f"aud-{name}", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                    "config": {"included.client.audience": name, "access.token.claim": "true"}}]})
            realm["roles"]["realm"].append({
                "name": f"{name}-access",
                "description": f"Human access to the {name} API (/api/{name}, all methods). Per-service access role — gates API access, not rows. Mirrors fm_runtime/grants.py + deploy/policy/data.json."})
            for r in realm["roles"]["realm"]:
                if r["name"] == "admin":
                    r["composites"]["realm"].append(f"{name}-access")
            client = {
                "clientId": name, "name": f"{name.capitalize()} backend service identity (scaffolded by new-service)",
                "publicClient": False, "secret": secret,
                "standardFlowEnabled": False, "directAccessGrantsEnabled": False, "serviceAccountsEnabled": True,
                "attributes": {"standard.token.exchange.enabled": "true",
                               "standard.token.exchange.enableRefreshRequestedTokenType": "SAME_SESSION"},
                "defaultClientScopes": ["basic", "profile", "roles", "fm-origin"]}
            if spec["deps"]:
                client["optionalClientScopes"] = [f"svc-{d}" for d in spec["deps"]]
            realm["clients"].append(client)
            by_id = {c["clientId"]: c for c in realm["clients"]}
            for cc in caller_clients:
                by_id[cc].setdefault("optionalClientScopes", [])
                if f"svc-{name}" not in by_id[cc]["optionalClientScopes"]:
                    by_id[cc]["optionalClientScopes"].append(f"svc-{name}")
            if browser:
                fe = by_id["frontend"]
                fe.setdefault("protocolMappers", [])
                fe["protocolMappers"].append({
                    "name": f"aud-{name}", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                    "config": {"included.custom.audience": name, "access.token.claim": "true", "id.token.claim": "false"}})
        return _m

    edit_json("deploy/keycloak/realm-funnelmanager-dev.json", mutate_realm(dev_secret))
    edit_json("deploy/keycloak/realm-funnelmanager-prod.example.json", mutate_realm(prod_secret))

    # ---- 8. grants.py -------------------------------------------------------
    dep_grants = "".join(f'\n        {{"service": "{d}", "methods": ["*"], "path_prefix": "/api/{d}"}},' for d in spec["deps"])
    insert_after("libs/fm_runtime/fm_runtime/grants.py",
                 "_DEFAULT_ROLE_GRANTS: dict[str, list[dict[str, Any]]] = {\n",
                 f'    "{name}-access": [\n'
                 f'        {{"service": "{name}", "methods": ["*"], "path_prefix": "/api/{name}"}},{dep_grants}\n    ],\n')
    insert_after("libs/fm_runtime/fm_runtime/grants.py", "SERVICES: tuple[str, ...] = (\n", f'    "{name}",\n')
    edges = "".join(f'    ("{c}", "{name}"),\n' for c in caller_clients)
    edges += "".join(f'    ("{name}", "{d}"),\n' for d in spec["deps"])
    if edges:
        insert_after("libs/fm_runtime/fm_runtime/grants.py",
                     "SVC_EXCHANGE_SCOPES: tuple[tuple[str, str], ...] = (\n", edges)

    # ---- 9. policy data.json -----------------------------------------------
    dep_role_grants = "".join(f'\n          {{"service": "{d}", "methods": ["*"], "path_prefix": "/api/{d}"}},' for d in spec["deps"])
    insert_after("deploy/policy/data.json", '    "roles": {\n',
                 f'      "{name}-access": {{\n'
                 f'        "description": "Human access to the {name} API. Full methods within /api/{name} (principle 1). Keep in sync with fm_runtime/grants.py.",\n'
                 f'        "grants": [\n'
                 f'          {{"service": "{name}", "methods": ["*"], "path_prefix": "/api/{name}"}}{("," + dep_role_grants) if spec["deps"] else ""}\n'
                 f'        ]\n      }},\n')
    caller_entries = []
    if browser:
        caller_entries.append('          {"ns": "istio-ingress", "sa": "istio-ingress"}')
    for c in spec["callers"]:
        if c.get("path_prefix"):
            caller_entries.append(f'          {{"ns": "env", "sa": "{c["from"]}", "path_prefixes": ["{c["path_prefix"]}"]}}')
        else:
            caller_entries.append(f'          {{"ns": "env", "sa": "{c["from"]}"}}')
    insert_after("deploy/policy/data.json", '      "callers": {\n',
                 f'        "{name}": [\n' + ",\n".join(caller_entries) + "\n        ],\n")
    for d in spec["deps"]:
        edit("deploy/policy/data.json", f'        "{d}": [\n',
             f'        "{d}": [\n          {{"ns": "env", "sa": "{name}"}},\n')
    azp_list = ", ".join('"%s"' % a for a in azp)
    insert_after("deploy/policy/data.json", '      "azp_allow": {\n',
                 f'        "{name}": [{azp_list}],\n')
    for d in spec["deps"]:
        edit_re("deploy/policy/data.json", rf'("{d}": \[)([^\]]*)\]', rf'\1\2, "{name}"]', count=1)
    if browser:
        insert_after("deploy/policy/data.json", '            "/health",\n', f'            "/api/{name}/health",\n')
        insert_after("deploy/policy/data.json", '      "routes": {\n', f'        "/api/{name}/": "{name}",\n')
        if ui:
            insert_after("deploy/policy/data.json", '      "routes": {\n', f'        "/{name}/": "{name}ui",\n')
    if stateful:
        insert_after("deploy/policy/data.json", '        {"from": "search", "to": "app-db"},\n',
                     f'        {{"from": "{name}", "to": "{name}-db"}},\n')

    # ---- 10. CI -------------------------------------------------------------
    insert_before(".github/workflows/build-images.yml", "          - { service: backup,",
                  f"          - {{ service: {name}, context: ., dockerfile: {name}/Dockerfile }}\n")
    insert_after(".github/workflows/build-canary.yml", "          - agents\n", f"          - {name}\n")
    edit_re(".github/workflows/build-canary.yml", r"(mcp\|mail\|jobs\|agents[a-z|]*)\)\n", rf"\1|{name})\n", count=1)
    if browser:
        edit(".github/workflows/build-canary.yml", ") rc=gateway ;;", f"|{name}) rc=gateway ;;", count=0)
    else:
        edit(".github/workflows/build-canary.yml", ") rc=eastwest ;;", f"|{name}) rc=eastwest ;;", count=0)
    edit_re(".github/workflows/ci.yml", r"-r mcp/requirements\.txt", f"-r mcp/requirements.txt -r {name}/requirements.txt", count=1)
    edit_re(".github/workflows/ci.yml", r"(for svc in [a-z ]+)(; do)", rf"\1 {name}\2", count=1)

    # ---- 11. skill service-lists -------------------------------------------
    edit_re(".claude/skills/deploy-funnelmanager/deploy.sh", r'(SERVICES="[a-z ]+)"', rf'\1 {name}"', count=1)
    edit_re(".claude/skills/prod-health/check.sh", r'(BACKENDS="[a-z ]+)"', rf'\1 {name}"', count=1)
    edit(".claude/skills/canary/canary.sh", ") echo backend ;;", f"|{name}) echo backend ;;")
    if browser:
        edit(".claude/skills/canary/canary.sh", ") echo gateway ;;", f"|{name}) echo gateway ;;")
    else:
        edit(".claude/skills/canary/canary.sh", ") echo eastwest ;;", f"|{name}) echo eastwest ;;")

    # ---- 12. CONVENTIONS.md -------------------------------------------------
    insert_after("deploy/CONVENTIONS.md", "| agents | 50m / 128Mi | 500m / 768Mi |\n",
                 f"| {name} | {RES['cpu_req']} / {RES['mem_req']} | {RES['cpu_lim']} / {RES['mem_lim']} |\n",
                 required=False)
    edit_re("deploy/CONVENTIONS.md", r"(`frontend`,)", rf"`{name}`, \1", count=1, required=False)
    edit_re("deploy/CONVENTIONS.md", r"(, )(static nginx 8080)", rf"\1{name} {port}, \2", count=1, required=False)
    if stateful:
        edit("deploy/CONVENTIONS.md", "`agents-db` (CNPG),", f"`agents-db` (CNPG), `{name}-db` (CNPG),", required=False)
        edit("deploy/CONVENTIONS.md", "agents-db 5Gi,", f"agents-db 5Gi, {name}-db 5Gi,", required=False)

    # ---- 13. agent doc ------------------------------------------------------
    desc_surface = f" (browser-facing at /api/{name})" if browser else " (internal only)"
    db_note = f", db funnelmanager_{name}" if stateful else ""
    db_inv = "\n- Init the DB in a FastAPI lifespan (see `app/database.py`)." if stateful else ""
    surface_inv = ""
    if spec["callers"]:
        surface_inv = "\n- Internal callers exchange toward you: " + ", ".join(
            f"`{c['from']}->{name}`" for c in spec["callers"]) + " (svc scopes + azp_allow + OPA callers)."
    emit(f".claude/agents/{name}-agent.md",
         sub(TPL_AGENT_MD, dict(ctx, DESC_SURFACE=desc_surface, DB_NOTE=db_note, DB_INV=db_inv, SURFACE_INV=surface_inv)))

    # ---- 14. Flux healthCheck ----------------------------------------------
    apps = _p("deploy/clusters/prod/apps.yaml")
    lines = apps.read_text().splitlines(keepends=True)
    idx = max(i for i, ln in enumerate(lines) if "namespace: prod }" in ln)
    lines.insert(idx + 1, f"    - {{ apiVersion: apps/v1, kind: Deployment, name: {name}, namespace: prod }}\n")
    apps.write_text("".join(lines))
    modified.add("deploy/clusters/prod/apps.yaml")

    # ---- 15. browser-surface legs ------------------------------------------
    if browser:
        rule = (f"    # {name} API (scaffolded by new-service).\n    - matches:\n"
                f"        - path: {{ type: PathPrefix, value: /api/{name}/ }}\n"
                f"      backendRefs:\n        - name: {name}\n          namespace: prod\n          port: {port}\n")
        if ui:
            rule += (f"    # {name} SPA (its own <{name}ui> container is a separate scaffold).\n    - matches:\n"
                     f"        - path: {{ type: PathPrefix, value: /{name}/ }}\n"
                     f"      backendRefs:\n        - name: {name}ui\n          namespace: prod\n          port: 8080\n")
        insert_before("deploy/infrastructure/gateway/httproutes.yaml", "    # Browser RUM ingest (Grafana Faro", rule)
        insert_after("frontend/nginx.dev.conf", "upstream funnel_searchui {\n    server searchui:5173;\n}\n",
                     f"\nupstream funnel_{name} {{\n    server {name}:{port};\n}}\n")
        loc = (f"    # {name} backend (scaffolded by new-service).\n"
               f"    location /api/{name}/ {{\n"
               f"        proxy_pass http://funnel_{name}/api/{name}/;\n"
               "        proxy_http_version 1.1;\n"
               "        proxy_set_header Host $host;\n"
               "        proxy_set_header X-Real-IP $remote_addr;\n"
               "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
               "        proxy_set_header X-Forwarded-Proto $scheme;\n"
               "        proxy_buffering off;\n        proxy_cache off;\n        proxy_read_timeout 300s;\n    }\n\n")
        insert_before("frontend/nginx.dev.conf", "    # App + Vite HMR", loc)
        if ui:
            edit_re("deploy/apps/overlays/prod/kustomization.yaml", r"(- 'web-apps=\[)",
                    rf'''\1{{"name":"{name.capitalize()}","description":"{name} app","url":"/{name}/","probe":"/api/{name}/whoami"}},''',
                    count=1, required=False)
        emit(f"deploy/infrastructure/gateway/canary/{name}-canary.yaml", sub(TPL_GW_CANARY, ctx))
    else:
        emit(f"deploy/infrastructure/mesh-policies/canary/{name}-canary-eastwest.yaml", sub(TPL_EW_CANARY, ctx))


# ==========================================================================
# verification
# ==========================================================================
def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def verify() -> bool:
    ok = True
    print("\n== verification ==")
    for label, realm in (("dev", "realm-funnelmanager-dev.json"), ("prod", "realm-funnelmanager-prod.example.json")):
        rc, out = run([sys.executable, "-m", "fm_runtime.export", "--check", "deploy/policy/data.json",
                       "--realm", f"deploy/keycloak/{realm}"])
        print(f"[{'PASS' if rc == 0 else 'FAIL'}] fm_runtime.export --check ({label} realm): {out}")
        ok &= rc == 0
    if shutil.which("kubectl"):
        for d in ("deploy/apps/overlays/prod", "deploy/infrastructure/gateway",
                  "deploy/infrastructure/mesh-policies"):
            rc, out = run(["kubectl", "kustomize", "--load-restrictor=LoadRestrictionsNone", d])
            print(f"[{'PASS' if rc == 0 else 'FAIL'}] kubectl kustomize {d}" + ("" if rc == 0 else f"\n{out[-1500:]}"))
            ok &= rc == 0
    else:
        print("[SKIP] kubectl not found")
    if shutil.which("docker"):
        # Mirror ci.yml's compose-validate job: dev needs a (gitignored) .env to
        # exist; prod guards KEYCLOAK_REALM_FILE with ${...:?} so a placeholder
        # satisfies interpolation for a parse-only check.
        (ROOT / ".env").touch()
        import os
        for cf in ("docker-compose.dev.yml", "docker-compose.prod.yml"):
            env = dict(os.environ, KEYCLOAK_REALM_FILE="/tmp/realm.json")
            proc = subprocess.run(["docker", "compose", "-f", cf, "config", "-q"], cwd=ROOT,
                                  capture_output=True, text=True, env=env)
            rc = proc.returncode
            print(f"[{'PASS' if rc == 0 else 'FAIL'}] docker compose -f {cf} config -q"
                  + ("" if rc == 0 else f"\n{(proc.stdout + proc.stderr).strip()[-800:]}"))
            ok &= rc == 0
    else:
        print("[SKIP] docker not found (compose parse is CI-verified)")
    if shutil.which("opa"):
        rc, out = run(["opa", "test", "deploy/policy"])
        print(f"[{'PASS' if rc == 0 else 'FAIL'}] opa test deploy/policy: {out.splitlines()[-1] if out else ''}")
        ok &= rc == 0
    else:
        print("[SKIP] opa not installed locally — `opa test deploy/policy` is CI-verified (ci.yml `policy` job)")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", help="path to a JSON (or YAML) spec file")
    ap.add_argument("--name")
    ap.add_argument("--port", type=int)
    ap.add_argument("--stateful", action="store_true")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--ui", action="store_true")
    ap.add_argument("--caller", action="append", help="from[:path_prefix]; repeatable")
    ap.add_argument("--dep", action="append", help="dependency service name; repeatable")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--manifest-out", help="write created/modified file lists as JSON here")
    args = ap.parse_args()

    spec = load_spec(args)
    print(f"scaffolding '{spec['name']}' (:{spec['port']}) stateful={spec['stateful']} "
          f"browser={spec['browser']} ui={spec['ui']} callers={[c['from'] for c in spec['callers']]} deps={spec['deps']}")
    generate(spec)
    print(f"\ncreated {len(created)} files, modified {len(modified)} files")
    if args.manifest_out:
        Path(args.manifest_out).write_text(json.dumps({"created": created, "modified": sorted(modified)}, indent=2))
    ok = True if args.no_verify else verify()
    print("\n" + ("ALL GATES PASS" if ok else "SOME GATES FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
