"""Export the code-owned security surface as JSON, so the OPA policy data and
the mesh config can be generated from / diffed against the code (one source of
truth — code and policy cannot drift).

Two modes:

- Anonymous allowlist (per service, needs the app to enumerate routes)::

      python -m fm_runtime.export app.main:app --service leads

  Phase 2 aggregates these into the OPA anonymous-path allowlist.

- Policy mirror (no app needed — the built-in services / svc-exchange scopes /
  role-grant table this library ships)::

      python -m fm_runtime.export

  Dumps the mirror as JSON for inspection.

- Policy check (the real lockstep assertion — exits nonzero on drift)::

      python -m fm_runtime.export --check deploy/policy/data.json \
          [--realm deploy/keycloak/realm-funnelmanager-dev.json]

  Verifies SVC_EXCHANGE_SCOPES ⇔ data.json ``azp_allow`` (bijection, modulo
  browser clients) and the built-in role grants ⇔ ``funnelmanager.roles``. With
  ``--realm`` it also fails on any client holding a ``svc-<x>`` scope not in the
  one-hop allowlist (a least-privilege over-grant). Wire this into CI so code,
  OPA data, and the realm can never silently drift.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from fm_runtime.annotations import collect_anonymous
from fm_runtime.grants import policy_mirror, verify_policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "app",
        nargs="?",
        help="ASGI app path, e.g. app.main:app. Omit to dump the policy mirror.",
    )
    parser.add_argument("--service", help="logical service name (with an app)")
    parser.add_argument(
        "--check",
        metavar="DATA_JSON",
        help="Verify the built-in policy against a deploy/policy/data.json; "
        "exit nonzero on any drift / over-grant.",
    )
    parser.add_argument(
        "--realm",
        metavar="REALM_JSON",
        help="With --check, also verify a Keycloak realm's svc-* client scopes "
        "against the one-hop exchange allowlist.",
    )
    args = parser.parse_args()

    if args.check:
        with open(args.check, encoding="utf-8") as fh:
            data = json.load(fh)
        realm = None
        if args.realm:
            with open(args.realm, encoding="utf-8") as fh:
                realm = json.load(fh)
        errors = verify_policy(data, realm)
        if errors:
            print("POLICY DRIFT / OVER-GRANT DETECTED:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        scope = "SVC_EXCHANGE_SCOPES ⇔ azp_allow, role grants ⇔ data.json"
        if realm is not None:
            scope += ", realm svc-* scopes ⇔ allowlist"
        print(f"policy in sync: {scope}")
        return 0

    if not args.app:
        # Policy mirror — services, svc-exchange scopes, role grants.
        json.dump(policy_mirror(), sys.stdout, indent=2, sort_keys=True)
        print()
        return 0

    if not args.service:
        parser.error("--service is required when exporting an app's anonymous allowlist")

    module_name, _, attr = args.app.partition(":")
    app = getattr(importlib.import_module(module_name), attr or "app")
    json.dump(collect_anonymous(app, args.service), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
