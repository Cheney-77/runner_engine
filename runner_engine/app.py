from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .admin_http import RunnerAdminHTTP
from .catalog import Catalog
from .gateway import AccessControl, serve
from .lifecycle_runtime import (
    LifecycleOpenSandboxBackend,
    LifecycleWorkerPool,
    RunnerLifecycleController,
)
from .lifecycle_service import LifecycleRunnerService
from .lifecycle_store import RuntimeLifecycleStore
from .policy import load_policies
from .quota import Quota
from .state import RunnerDB

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed Python Runner v3.3")
    parser.add_argument(
        "--listen",
        default=os.environ.get("RUNNER_LISTEN", "0.0.0.0:9443"),
    )
    parser.add_argument(
        "--catalog",
        default=os.environ.get("RUNNER_CATALOG", "./catalog"),
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("RUNNER_DB_URL", ""),
    )
    parser.add_argument(
        "--policies",
        default=os.environ.get(
            "RUNNER_POLICIES",
            "./config/policies.json",
        ),
    )
    parser.add_argument(
        "--acl",
        default=os.environ.get("RUNNER_ACL", "./config/acl.json"),
    )
    parser.add_argument(
        "--clusters",
        default=os.environ.get(
            "RUNNER_CLUSTERS",
            "./config/opensandbox.json",
        ),
    )
    parser.add_argument(
        "--owner-id",
        default=os.environ.get("RUNNER_OWNER_ID", ""),
    )
    parser.add_argument(
        "--tls-cert",
        default=os.environ.get("RUNNER_TLS_CERT", ""),
    )
    parser.add_argument(
        "--tls-key",
        default=os.environ.get("RUNNER_TLS_KEY", ""),
    )
    parser.add_argument(
        "--client-ca",
        default=os.environ.get("RUNNER_CLIENT_CA", ""),
    )
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

    clusters = json.loads(
        Path(args.clusters).read_text(encoding="utf-8")
    )
    policies = load_policies(args.policies)

    lifecycle = RuntimeLifecycleStore(args.db_url)
    backend = LifecycleOpenSandboxBackend(
        clusters,
        owner_id=args.owner_id,
    )
    quota = Quota(
        max_creating=int(os.environ.get("RUNNER_MAX_CREATING", "8")),
        max_live=int(os.environ.get("RUNNER_MAX_LIVE", "64")),
        max_live_per_tenant=int(
            os.environ.get("RUNNER_MAX_LIVE_PER_TENANT", "16")
        ),
    )
    pool = LifecycleWorkerPool(
        backend,
        quota,
        lifecycle=lifecycle,
        idle_seconds=int(
            os.environ.get("RUNNER_WORKER_IDLE_SECONDS", "600")
        ),
        orphan_ttl_seconds=int(
            os.environ.get(
                "RUNNER_SANDBOX_ORPHAN_TTL_SECONDS",
                "3600",
            )
        ),
        renew_before_seconds=int(
            os.environ.get(
                "RUNNER_SANDBOX_RENEW_BEFORE_SECONDS",
                "900",
            )
        ),
        max_installed_releases=int(
            os.environ.get(
                "RUNNER_MAX_RELEASES_PER_SANDBOX",
                "256",
            )
        ),
    )
    service = LifecycleRunnerService(
        Catalog(args.catalog),
        RunnerDB(args.db_url),
        pool,
        policies,
        lifecycle=lifecycle,
        lease_ttl_ms=int(
            os.environ.get(
                "RUNNER_LEASE_TTL_MS",
                str(15 * 60 * 1000),
            )
        ),
        idempotency_retention_ms=int(
            os.environ.get(
                "RUNNER_IDEMPOTENCY_RETENTION_MS",
                str(7 * 24 * 60 * 60 * 1000),
            )
        ),
        run_retention_ms=int(
            os.environ.get(
                "RUNNER_RUN_RETENTION_MS",
                str(90 * 24 * 60 * 60 * 1000),
            )
        ),
    )

    admin_server = None
    grpc_server = None

    try:
        service.startup()
        service.start_background(
            interval_seconds=int(
                os.environ.get(
                    "RUNNER_REAPER_INTERVAL_SECONDS",
                    "30",
                )
            )
        )

        controller = RunnerLifecycleController(
            service,
            lifecycle,
        )
        admin_token = os.environ.get(
            "RUNNER_ADMIN_TOKEN",
            "",
        ).strip()

        if admin_token:
            admin_server = RunnerAdminHTTP(
                controller,
                host=os.environ.get(
                    "RUNNER_ADMIN_LISTEN",
                    "127.0.0.1",
                ),
                port=int(
                    os.environ.get(
                        "RUNNER_ADMIN_PORT",
                        "9444",
                    )
                ),
                token=admin_token,
            )
            admin_server.start()
            logger.info(
                "Runner lifecycle admin HTTP listening on %s:%s",
                os.environ.get(
                    "RUNNER_ADMIN_LISTEN",
                    "127.0.0.1",
                ),
                os.environ.get(
                    "RUNNER_ADMIN_PORT",
                    "9444",
                ),
            )
        else:
            logger.warning(
                "RUNNER_ADMIN_TOKEN is empty; lifecycle Admin/Observe "
                "HTTP is disabled"
            )

        grpc_server = serve(
            service,
            AccessControl.from_json(args.acl),
            address=args.listen,
            cert_file=args.tls_cert,
            key_file=args.tls_key,
            client_ca_file=args.client_ca,
        )
        grpc_server.wait_for_termination()
    finally:
        if admin_server is not None:
            admin_server.close()
        if grpc_server is not None:
            grpc_server.stop(grace=5)
        service.shutdown()
        lifecycle.close()


if __name__ == "__main__":
    main()
