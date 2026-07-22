#!/bin/bash
# Dump funnelmanager_leads from the in-namespace mongo pod and push a gzipped
# archive to object storage, then prune archives older than the retention
# window. mongodump runs INSIDE the mongo pod (localhost, no mesh hop);
# kubectl streams the archive out and `mc pipe` uploads it.
set -euo pipefail

: "${NAMESPACE:?}" "${MONGO_POD:?}" "${BUCKET:?}" "${S3_ENDPOINT:?}"
: "${ACCESS_KEY_ID:?}" "${ACCESS_SECRET_KEY:?}"
RETENTION="${RETENTION:-3d}"
export MC_CONFIG_DIR=/tmp/.mc

TS="$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="obj/${BUCKET}/mongo-leads/${NAMESPACE}"
OBJ="${PREFIX}/leads-${TS}.archive.gz"

mc alias set obj "${S3_ENDPOINT}" "${ACCESS_KEY_ID}" "${ACCESS_SECRET_KEY}" >/dev/null

echo "dumping funnelmanager_leads from ${NAMESPACE}/${MONGO_POD} -> ${OBJ}"
kubectl exec -n "${NAMESPACE}" "${MONGO_POD}" -c mongo -- \
  mongodump --host 127.0.0.1 --db funnelmanager_leads --archive --gzip \
  | mc pipe "${OBJ}"

echo "verifying upload..."
mc stat "${OBJ}" >/dev/null
echo "uploaded ${OBJ}"

echo "pruning archives older than ${RETENTION}"
mc rm --recursive --force --older-than "${RETENTION}" "${PREFIX}/" || true

echo "backup complete"
