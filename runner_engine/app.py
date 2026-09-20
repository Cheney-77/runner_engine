from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .catalog import Catalog
from .gateway import AccessControl, serve
from .policy import load_policies
from .pool import WorkerPool
from .quota import Quota
from .sandbox.opensandbox import OpenSandboxBackend
from .service import RunnerService
from .state import RunnerDB


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed Python Runner v3.3")
    parser.add_argument("--listen", default=os.environ.get("RUNNER_LISTEN", "0.0.0.0:9443"))
    parser.add_argument("--catalog", default=os.environ.get("RUNNER_CATALOG", "./catalog"))
    parser.add_argument("--db-url", default=os.environ.get("RUNNER_DB_URL", ""))
    parser.add_argument("--policies", default=os.environ.get("RUNNER_POLICIES", "./config/policies.json"))
    parser.add_argument("--acl", default=os.environ.get("RUNNER_ACL", "./config/acl.json"))
    parser.add_argument("--clusters", default=os.environ.get("RUNNER_CLUSTERS", "./config/opensandbox.json"))
    parser.add_argument("--owner-id", default=os.environ.get("RUNNER_OWNER_ID", ""))
    parser.add_argument("--tls-cert", default=os.environ.get("RUNNER_TLS_CERT", ""))
    parser.add_argument("--tls-key", default=os.environ.get("RUNNER_TLS_KEY", ""))
    parser.add_argument("--client-ca", default=os.environ.get("RUNNER_CLIENT_CA", ""))
    args = parser.parse_args()

    required = {
        "db-url": args.db_url,
        "owner-id": args.owner_id,
        "tls-cert": args.tls_cert,
        "tls-key": args.tls_key,
        "client-ca": args.client_ca,
    }
    for name, value in required.items():
        if not value:
            raise SystemExit(f"--{name} is required")

    clusters = json.loads(Path(args.clusters).read_text(encoding="utf-8"))
    policies = load_policies(args.policies)

    backend = OpenSandboxBackend(clusters, owner_id=args.owner_id)
    quota = Quota(
        max_creating=int(os.environ.get("RUNNER_MAX_CREATING", "8")),
        max_live=int(os.environ.get("RUNNER_MAX_LIVE", "64")),
        max_live_per_tenant=int(os.environ.get("RUNNER_MAX_LIVE_PER_TENANT", "16")),
    )
    pool = WorkerPool(
        backend,
        quota,
        idle_seconds=int(os.environ.get("RUNNER_WORKER_IDLE_SECONDS", "600")),
        orphan_ttl_seconds=int(os.environ.get("RUNNER_SANDBOX_ORPHAN_TTL_SECONDS", "3600")),
        renew_before_seconds=int(os.environ.get("RUNNER_SANDBOX_RENEW_BEFORE_SECONDS", "900")),
        max_installed_releases=int(os.environ.get("RUNNER_MAX_RELEASES_PER_SANDBOX", "256")),
    )

    service = RunnerService(
        Catalog(args.catalog),
        RunnerDB(args.db_url),
        pool,
        policies,
        lease_ttl_ms=int(os.environ.get("RUNNER_LEASE_TTL_MS", str(15 * 60 * 1000))),
        idempotency_retention_ms=int(os.environ.get("RUNNER_IDEMPOTENCY_RETENTION_MS", str(7 * 24 * 60 * 60 * 1000))),
        run_retention_ms=int(os.environ.get("RUNNER_RUN_RETENTION_MS", str(90 * 24 * 60 * 60 * 1000))),
    )

    service.startup()
    service.start_background(
        interval_seconds=int(os.environ.get("RUNNER_REAPER_INTERVAL_SECONDS", "30"))
    )

    server = serve(
        service,
        AccessControl.from_json(args.acl),
        address=args.listen,
        cert_file=args.tls_cert,
        key_file=args.tls_key,
        client_ca_file=args.client_ca,
    )

    try:
        server.wait_for_termination()
    finally:
        server.stop(grace=5)
        service.shutdown()


if __name__ == "__main__":
    main()
