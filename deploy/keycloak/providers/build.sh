#!/usr/bin/env bash
# Deterministic build of the fm-origin Keycloak script-provider JAR from src/,
# plus regeneration of the k3s ConfigMap that ships it to the cluster.
#
# The JAR is "just a zip of src/". This build is byte-reproducible on any POSIX
# host (macOS dev + Ubuntu CI): entries are STORED (no deflate, so the output
# does not depend on the local zlib version), emitted in a fixed order with a
# fixed 1980-01-01 timestamp. That reproducibility is load-bearing — CI runs
# this same script and fails the build if the committed fm-origin-provider.jar
# or providers-configmap.yaml differ from a fresh build (see .github/workflows).
#
# Run it after editing anything under src/:
#   deploy/keycloak/providers/build.sh
# then commit the regenerated JAR + ConfigMap. On a release, CI regenerates and
# commits them for you, exactly like the sha-image pins.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"

jar="fm-origin-provider.jar"
configmap="../../infrastructure/identity/providers-configmap.yaml"

# 1. Build the JAR deterministically from src/.
python3 - "$jar" <<'PY'
import sys, zipfile

jar = sys.argv[1]
# (archive name, source path) in a FIXED order. STORED (no compression) keeps the
# bytes independent of the host's zlib; a fixed 1980-01-01 date_time removes the
# only other source of nondeterminism. create_system is 3 (Unix) on every POSIX
# host, so macOS and Ubuntu produce identical bytes.
entries = [
    ("META-INF/keycloak-scripts.json", "src/META-INF/keycloak-scripts.json"),
    ("fm-origin-passthrough.js",       "src/fm-origin-passthrough.js"),
]
with zipfile.ZipFile(jar, "w") as z:
    for arcname, path in entries:
        with open(path, "rb") as fh:
            data = fh.read()
        zi = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED
        zi.external_attr = 0o644 << 16
        z.writestr(zi, data)
PY

# 2. Regenerate the k3s ConfigMap: a fixed template + a fresh single-line base64
#    of the JAR (matches the `binaryData` shape kubectl create configmap emits,
#    but without depending on a kubectl version).
b64="$(base64 < "$jar" | tr -d '\n')"
cat > "$configmap" <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: keycloak-providers
  namespace: identity
  labels:
    app.kubernetes.io/name: keycloak
    app.kubernetes.io/part-of: funnelmanager
# GENERATED — do not hand-edit. Produced by deploy/keycloak/providers/build.sh
# from src/. fm-origin-provider.jar is a Keycloak script provider (feature
# 'scripts') whose fm-origin-passthrough mapper carries the fm_origin claim
# across RFC 8693 token exchanges. To change it, edit src/ then re-run build.sh;
# CI regenerates + verifies this file on release.
binaryData:
  fm-origin-provider.jar: ${b64}
YAML

echo "Built $jar ($(wc -c < "$jar" | tr -d ' ') bytes)"
echo "Regenerated $configmap"
