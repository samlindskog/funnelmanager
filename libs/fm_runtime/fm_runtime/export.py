"""Export a service's public-anonymous allowlist as JSON.

Run inside the service's environment (its venv or container):

    python -m fm_runtime.export app.main:app --service leads

Phase 2 aggregates these into the OPA policy data document, so the Rego
anonymous-path allowlist is generated from the code annotations — one source
of truth."""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from fm_runtime.annotations import collect_anonymous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", help="ASGI app path, e.g. app.main:app")
    parser.add_argument("--service", required=True, help="logical service name")
    args = parser.parse_args()

    module_name, _, attr = args.app.partition(":")
    app = getattr(importlib.import_module(module_name), attr or "app")
    json.dump(collect_anonymous(app, args.service), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
