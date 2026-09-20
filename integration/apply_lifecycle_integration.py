from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


NEW_FILES = (
    "build_service/harbor.py",
    "build_service/lifecycle.py",
    "build_service/lifecycle_service.py",
    "build_service/lifecycle_routes.py",
    "publish_service/runtime_lifecycle.py",
    "publish_service/lifecycle_service.py",
    "publish_service/lifecycle_routes.py",
    "runner_engine/lifecycle_store.py",
    "runner_engine/lifecycle_runtime.py",
    "runner_engine/admin_http.py",
    "web/admin/lifecycle_client.py",
    "web/admin/lifecycle_inventory.py",
    "web/admin/execution_store.py",
    "web/admin/cleanup.py",
    "web/admin/lifecycle_routes.py",
    "web/admin/static/lifecycle.js",
    "web/observe/runner_runtime.py",
    "web/observe/runtime_routes.py",
    "web/observe/static/runtime.js",
    "sql/002_admin_cleanup_execution.sql",
)


class IntegrationError(RuntimeError):
    pass


@dataclass
class Edit:
    path: str
    before: str
    after: str
    label: str


def replace_once(text: str, edit: Edit) -> tuple[str, bool]:
    if edit.after in text:
        return text, False

    count = text.count(edit.before)
    if count != 1:
        raise IntegrationError(
            f"{edit.path}: expected exactly one anchor for {edit.label}; "
            f"found {count}. Your branch differs from the verified GitHub main; "
            "merge this entry point manually instead of guessing."
        )

    return text.replace(edit.before, edit.after, 1), True


def build_edits() -> list[Edit]:
    return [
        Edit(
            "build_service/app.py",
            "from .service import BuildService, BuildSettings\n"
            "from .store import BuildStore\n",
            "from .harbor import HarborClient\n"
            "from .lifecycle_routes import install_build_lifecycle_routes\n"
            "from .lifecycle_service import LifecycleBuildService, LifecycleBuildStore\n"
            "from .service import BuildService, BuildSettings\n"
            "from .store import BuildStore\n",
            "Build lifecycle imports",
        ),
        Edit(
            "build_service/app.py",
            "    store = BuildStore(database_url)\n",
            "    store = LifecycleBuildStore(database_url)\n",
            "LifecycleBuildStore construction",
        ),
        Edit(
            "build_service/app.py",
            "    return BuildService(store, settings)\n",
            "    return LifecycleBuildService(\n"
            "        store,\n"
            "        settings,\n"
            "        harbor=HarborClient.from_env(),\n"
            "    )\n",
            "LifecycleBuildService construction",
        ),
        Edit(
            "build_service/app.py",
            "    return application\n\n\napp = create_app()\n",
            "    install_build_lifecycle_routes(application)\n"
            "    return application\n\n\napp = create_app()\n",
            "Build lifecycle routes",
        ),
    ]


def publish_edits() -> list[Edit]:
    return [
        Edit(
            "publish_service/app.py",
            "from .model import AnalyzeRequest, BackendRequest, CreateVirtualContractRequest\n",
            "from .lifecycle_routes import install_publish_lifecycle_routes\n"
            "from .lifecycle_service import LifecyclePublishService\n"
            "from .model import AnalyzeRequest, BackendRequest, CreateVirtualContractRequest\n",
            "Publish lifecycle imports",
        ),
        Edit(
            "publish_service/app.py",
            "    return PublishService",
            "    return LifecyclePublishService",
            "LifecyclePublishService construction",
        ),
        Edit(
            "publish_service/app.py",
            "    return application\n\n\napp = create_app()\n",
            "    install_publish_lifecycle_routes(application)\n"
            "    return application\n\n\napp = create_app()\n",
            "Publish lifecycle routes",
        ),
    ]


def runner_service_edits() -> list[Edit]:
    return [
        Edit(
            "runner_engine/service.py",
            "        run_retention_ms: int = DEFAULT_RUN_RETENTION_MS,\n"
            "    ):\n",
            "        run_retention_ms: int = DEFAULT_RUN_RETENTION_MS,\n"
            "        lifecycle=None,\n"
            "    ):\n",
            "Runner lifecycle constructor argument",
        ),
        Edit(
            "runner_engine/service.py",
            "        self.run_retention_ms = run_retention_ms\n"
            "        self._active: dict[str, ActiveRun] = {}\n",
            "        self.run_retention_ms = run_retention_ms\n"
            "        self.lifecycle = lifecycle\n"
            "        self._active: dict[str, ActiveRun] = {}\n",
            "Runner lifecycle field",
        ),
        Edit(
            "runner_engine/service.py",
            "        self.db.close()\n"
            "    def acquire_lease(\n",
            "        self.db.close()\n"
            "\n"
            "    def _assert_runtime_active(self, release) -> None:\n"
            "        if self.lifecycle is None:\n"
            "            return\n"
            "\n"
            "        row = self.lifecycle.get_by_runtime_id(release.runtime.id)\n"
            "        if row is None:\n"
            "            return\n"
            "\n"
            "        state = row[\"state\"]\n"
            "        raise RunnerError(\n"
            "            \"RUNTIME_RETIRED\" if state == \"RETIRED\" else \"RUNTIME_RETIRING\",\n"
            "            f\"runtime image is {state.lower()} and cannot accept new execution\",\n"
            "            retryable=state == \"RETIRING\",\n"
            "        )\n"
            "\n"
            "    def acquire_lease(\n",
            "Runner runtime gate helper",
        ),
        Edit(
            "runner_engine/service.py",
            "    ) -> Lease:\n"
            "        release = self.catalog.get(release_id)\n"
            "        if release.profile not in self.policies:\n",
            "    ) -> Lease:\n"
            "        release = self.catalog.get(release_id)\n"
            "        self._assert_runtime_active(release)\n"
            "        if release.profile not in self.policies:\n",
            "Lease acquire lifecycle gate",
        ),
        Edit(
            "runner_engine/service.py",
            "    def renew_lease(self, *, tenant_id: str, lease_id: str) -> Lease:\n"
            "        return self.db.renew_lease(\n"
            "            lease_id,\n"
            "            tenant_id=tenant_id,\n"
            "            ttl_ms=self.lease_ttl_ms,\n"
            "        )\n",
            "    def renew_lease(self, *, tenant_id: str, lease_id: str) -> Lease:\n"
            "        current = self.db.require_lease(\n"
            "            lease_id,\n"
            "            tenant_id=tenant_id,\n"
            "        )\n"
            "        release = self.catalog.get(current.release_id)\n"
            "        self._assert_runtime_active(release)\n"
            "        return self.db.renew_lease(\n"
            "            lease_id,\n"
            "            tenant_id=tenant_id,\n"
            "            ttl_ms=self.lease_ttl_ms,\n"
            "        )\n",
            "Lease renew lifecycle gate",
        ),
        Edit(
            "runner_engine/service.py",
            "        release = self.catalog.get(request.release_id)\n"
            "\n"
            "        try:\n"
            "            policy = self.policies[release.profile]\n",
            "        release = self.catalog.get(request.release_id)\n"
            "        self._assert_runtime_active(release)\n"
            "\n"
            "        try:\n"
            "            policy = self.policies[release.profile]\n",
            "Invoke preflight lifecycle gate",
        ),
        Edit(
            "runner_engine/service.py",
            "            worker = self.pool.acquire(\n"
            "                request.tenant_id,\n"
            "                release,\n"
            "                policy,\n"
            "            )\n"
            "            with self._active_lock:\n"
            "                if request.invocation_id in self._active:\n",
            "            worker = self.pool.acquire(\n"
            "                request.tenant_id,\n"
            "                release,\n"
            "                policy,\n"
            "            )\n"
            "            with self._active_lock:\n"
            "                self._assert_runtime_active(release)\n"
            "                if request.invocation_id in self._active:\n",
            "Invoke post-acquire lifecycle gate",
        ),
    ]


def runner_app_edits() -> list[Edit]:
    return [
        Edit(
            "runner_engine/app.py",
            "from .catalog import Catalog\n"
            "from .gateway import AccessControl, serve\n"
            "from .policy import load_policies\n"
            "from .pool import WorkerPool\n"
            "from .quota import Quota\n"
            "from .sandbox.opensandbox import OpenSandboxBackend\n"
            "from .service import RunnerService\n"
            "from .state import RunnerDB\n",
            "from .admin_http import RunnerAdminHTTP\n"
            "from .catalog import Catalog\n"
            "from .gateway import AccessControl, serve\n"
            "from .lifecycle_runtime import (\n"
            "    LifecycleOpenSandboxBackend,\n"
            "    LifecycleWorkerPool,\n"
            "    RunnerLifecycleController,\n"
            ")\n"
            "from .lifecycle_store import RuntimeLifecycleStore\n"
            "from .policy import load_policies\n"
            "from .pool import WorkerPool\n"
            "from .quota import Quota\n"
            "from .sandbox.opensandbox import OpenSandboxBackend\n"
            "from .service import RunnerService\n"
            "from .state import RunnerDB\n",
            "Runner lifecycle imports",
        ),
        Edit(
            "runner_engine/app.py",
            "    clusters = json.loads(Path(args.clusters).read_text(encoding=\"utf-8\"))\n"
            "    policies = load_policies(args.policies)\n"
            "    backend = OpenSandboxBackend(clusters, owner_id=args.owner_id)\n",
            "    clusters = json.loads(Path(args.clusters).read_text(encoding=\"utf-8\"))\n"
            "    policies = load_policies(args.policies)\n"
            "    lifecycle = RuntimeLifecycleStore(args.db_url)\n"
            "    backend = LifecycleOpenSandboxBackend(\n"
            "        clusters,\n"
            "        owner_id=args.owner_id,\n"
            "    )\n",
            "Runner lifecycle store/backend",
        ),
        Edit(
            "runner_engine/app.py",
            "    pool = WorkerPool(\n"
            "        backend,\n"
            "        quota,\n",
            "    pool = LifecycleWorkerPool(\n"
            "        backend,\n"
            "        quota,\n"
            "        lifecycle=lifecycle,\n",
            "LifecycleWorkerPool construction",
        ),
        Edit(
            "runner_engine/app.py",
            "        run_retention_ms=int(os.environ.get(\"RUNNER_RUN_RETENTION_MS\", str(90 * 24 * 60 * 60 * 1000))),\n"
            "    )\n",
            "        run_retention_ms=int(os.environ.get(\"RUNNER_RUN_RETENTION_MS\", str(90 * 24 * 60 * 60 * 1000))),\n"
            "        lifecycle=lifecycle,\n"
            "    )\n"
            "    lifecycle_controller = RunnerLifecycleController(service, lifecycle)\n",
            "RunnerService lifecycle wiring",
        ),
        Edit(
            "runner_engine/app.py",
            "    server = serve(\n"
            "        service,\n"
            "        AccessControl.from_json(args.acl),\n"
            "        address=args.listen,\n"
            "        cert_file=args.tls_cert,\n"
            "        key_file=args.tls_key,\n"
            "        client_ca_file=args.client_ca,\n"
            "    )\n"
            "\n"
            "    try:\n",
            "    server = serve(\n"
            "        service,\n"
            "        AccessControl.from_json(args.acl),\n"
            "        address=args.listen,\n"
            "        cert_file=args.tls_cert,\n"
            "        key_file=args.tls_key,\n"
            "        client_ca_file=args.client_ca,\n"
            "    )\n"
            "\n"
            "    admin_server = None\n"
            "    admin_token = os.environ.get(\"RUNNER_ADMIN_TOKEN\", \"\")\n"
            "    if admin_token:\n"
            "        admin_server = RunnerAdminHTTP(\n"
            "            lifecycle_controller,\n"
            "            host=os.environ.get(\"RUNNER_ADMIN_LISTEN\", \"127.0.0.1\"),\n"
            "            port=int(os.environ.get(\"RUNNER_ADMIN_PORT\", \"9444\")),\n"
            "            token=admin_token,\n"
            "        )\n"
            "        admin_server.start()\n"
            "\n"
            "    try:\n",
            "Runner admin management endpoint",
        ),
        Edit(
            "runner_engine/app.py",
            "    finally:\n"
            "        server.stop(grace=5)\n"
            "        service.shutdown()\n",
            "    finally:\n"
            "        if admin_server is not None:\n"
            "            admin_server.close()\n"
            "        server.stop(grace=5)\n"
            "        service.shutdown()\n"
            "        lifecycle.close()\n",
            "Runner lifecycle shutdown",
        ),
    ]


def admin_edits() -> list[Edit]:
    return [
        Edit(
            "web/admin/main.py",
            "from .inventory import Inventory\n"
            "from .store import AdminStore, Conflict, StorageUnavailable\n",
            "from .inventory import Inventory\n"
            "from .lifecycle_inventory import LifecycleInventory\n"
            "from .lifecycle_routes import install_lifecycle_routes\n"
            "from .store import AdminStore, Conflict, StorageUnavailable\n",
            "Admin lifecycle route import",
        ),
        Edit(
            "web/admin/main.py",
            "    app.state.inventory = inventory\n",
            "    app.state.inventory = inventory\n"
            "    lifecycle_inventory = LifecycleInventory(inventory)\n",
            "Admin lifecycle inventory",
        ),
        Edit(
            "web/admin/main.py",
            "            result = await asyncio.to_thread(inventory.cleanup_preview, environment_id)\n",
            "            result = await asyncio.to_thread(\n"
            "                lifecycle_inventory.cleanup_preview,\n"
            "                environment_id,\n"
            "            )\n",
            "Admin lifecycle preview",
        ),
        Edit(
            "web/admin/main.py",
            "            snapshot = await asyncio.to_thread(\n"
            "                inventory.cleanup_preview, body.environmentId\n"
            "            )\n",
            "            snapshot = await asyncio.to_thread(\n"
            "                lifecycle_inventory.cleanup_preview,\n"
            "                body.environmentId,\n"
            "            )\n",
            "Admin cleanup request preflight",
        ),
        Edit(
            "web/admin/main.py",
            "    app.mount(\"/admin/static\", StaticFiles(directory=STATIC), name=\"admin-static\")\n"
            "    return app\n",
            "    install_lifecycle_routes(\n"
            "        app,\n"
            "        inventory=inventory,\n"
            "        store=store,\n"
            "        audit_actor=audit_actor,\n"
            "    )\n"
            "    app.mount(\"/admin/static\", StaticFiles(directory=STATIC), name=\"admin-static\")\n"
            "    return app\n",
            "Admin lifecycle routes",
        ),
        Edit(
            "web/admin/static/index.html",
            '<script defer src="/admin/static/admin.js"></script>\n',
            '<script defer src="/admin/static/admin.js"></script>\n'
            '<script defer src="/admin/static/lifecycle.js"></script>\n',
            "Admin lifecycle JavaScript",
        ),
    ]



def admin_inventory_edits() -> list[Edit]:
    return [
        Edit(
            "web/admin/inventory.py",
            '                """SELECT id::text, env_key, status, image_ref,\n'
            '                          python_version, platform, package_count, created_at, updated_at\n'
            '                   FROM build.runtime_environments\n'
            '                   ORDER BY updated_at DESC LIMIT 100""",\n'
            '                ("id","envKey","status","imageRef","pythonVersion",\n'
            '                 "platform","packageCount","createdAt","updatedAt"),\n',
            '                """SELECT id::text, env_key, status, image_ref,\n'
            '                          python_version, platform, package_count,\n'
            '                          lifecycle_state, created_at, updated_at\n'
            '                   FROM build.runtime_environments\n'
            '                   ORDER BY updated_at DESC LIMIT 100""",\n'
            '                ("id","envKey","status","imageRef","pythonVersion",\n'
            '                 "platform","packageCount","lifecycleState",\n'
            '                 "createdAt","updatedAt"),\n',
            "Admin Build lifecycle inventory column",
        ),
        Edit(
            "web/admin/static/admin.js",
            '    rows("envRows",environments,item=>[\n'
            '      code(item.envKey),tag(item.status),\n'
            '      code(item.imageRef),number(item.packageCount),date(item.updatedAt),\n'
            '      environmentActions(item),\n'
            '    ],6);\n',
            '    rows("envRows",environments,item=>[\n'
            '      code(item.envKey),\n'
            '      tag(item.lifecycleState && item.lifecycleState!=="ACTIVE"\n'
            '        ?item.lifecycleState:item.status),\n'
            '      code(item.imageRef),number(item.packageCount),date(item.updatedAt),\n'
            '      environmentActions(item),\n'
            '    ],6);\n',
            "Admin environment lifecycle display",
        ),
        Edit(
            "web/admin/static/admin.js",
            '      [r.envKey,r.status,r.imageRef].join(" ").toLowerCase().includes(query)\n',
            '      [r.envKey,r.status,r.lifecycleState,r.imageRef]\n'
            '        .join(" ").toLowerCase().includes(query)\n',
            "Admin lifecycle-aware environment search",
        ),
        Edit(
            "web/admin/static/admin.js",
            '        `${env.envKey} · ${env.status} · ${env.id.slice(0,8)}`);\n',
            '        `${env.envKey} · ${env.lifecycleState||env.status} · ${env.id.slice(0,8)}`);\n',
            "Admin lifecycle-aware environment picker",
        ),
    ]

def observe_edits() -> list[Edit]:
    return [
        Edit(
            "web/observe/main.py",
            "from .database import readers\n"
            "from .sandbox import SandboxReader\n",
            "from .database import readers\n"
            "from .runtime_routes import install_runner_runtime_routes\n"
            "from .sandbox import SandboxReader\n",
            "Observe Runner runtime import",
        ),
        Edit(
            "web/observe/main.py",
            "    app.state.sandbox_reader = sandbox_reader or SandboxReader(settings)\n"
            "    @app.middleware(\"http\")\n",
            "    app.state.sandbox_reader = sandbox_reader or SandboxReader(settings)\n"
            "    install_runner_runtime_routes(app)\n"
            "    @app.middleware(\"http\")\n",
            "Observe Runner runtime route",
        ),
        Edit(
            "web/observe/static/index.html",
            '  <script defer src="/observe/static/dashboard.js"></script>\n',
            '  <script defer src="/observe/static/dashboard.js"></script>\n'
            '  <script defer src="/observe/static/runtime.js"></script>\n',
            "Observe Runner runtime JavaScript",
        ),
    ]


def all_edits() -> list[Edit]:
    return (
        build_edits()
        + publish_edits()
        + runner_service_edits()
        + runner_app_edits()
        + admin_edits()
        + admin_inventory_edits()
        + observe_edits()
    )


def check_repo(repo: Path) -> None:
    required = {
        edit.path
        for edit in all_edits()
    }

    for relative in sorted(required):
        path = repo / relative
        if not path.is_file():
            raise IntegrationError(f"required file is missing: {relative}")


def integrate(repo: Path, bundle_root: Path, *, write: bool) -> None:
    check_repo(repo)

    changes: dict[str, str] = {}
    changed_labels: list[str] = []

    by_path: dict[str, list[Edit]] = {}
    for edit in all_edits():
        by_path.setdefault(edit.path, []).append(edit)

    for relative, edits in by_path.items():
        path = repo / relative
        text = path.read_text(encoding="utf-8")

        for edit in edits:
            text, changed = replace_once(text, edit)
            if changed:
                changed_labels.append(edit.label)

        changes[relative] = text

    for relative in NEW_FILES:
        source = bundle_root / relative
        if not source.is_file():
            raise IntegrationError(
                f"bundle is incomplete; missing {relative}"
            )

    if not write:
        print("CHECK OK")
        print(f"entry files ready: {len(changes)}")
        print(f"new files ready: {len(NEW_FILES)}")
        print(f"pending edits: {len(changed_labels)}")
        return

    backup_root = repo / ".mpr-lifecycle-backup"
    backup_root.mkdir(exist_ok=True)

    for relative, text in changes.items():
        destination = repo / relative
        backup = backup_root / relative
        backup.parent.mkdir(parents=True, exist_ok=True)

        if not backup.exists():
            shutil.copy2(destination, backup)

        destination.write_text(text, encoding="utf-8")

    for relative in NEW_FILES:
        source = bundle_root / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    print("APPLIED")
    print(f"entry files changed: {len(changes)}")
    print(f"new files copied: {len(NEW_FILES)}")
    print(f"backup: {backup_root}")
    print()
    print("Next:")
    print("  1. apply sql/002_admin_cleanup_execution.sql to ADMIN_DB_URL")
    print("  2. configure lifecycle owner tokens and Harbor credentials")
    print("  3. run Python/JS tests before restarting services")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Integrate Resource Lifecycle v1 into Cheney-77/runner_engine. "
            "The script fails closed when verified source anchors are missing."
        )
    )
    parser.add_argument(
        "repo",
        type=Path,
        help="path to the runner_engine repository",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate anchors and bundle files without changing the repository",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    bundle_root = Path(__file__).resolve().parents[1]

    try:
        integrate(
            repo,
            bundle_root,
            write=not args.check,
        )
    except IntegrationError as exc:
        print(f"INTEGRATION REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
