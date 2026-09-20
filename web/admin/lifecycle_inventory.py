from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class LifecycleInventory:
    def __init__(self, inventory):
        self.inventory = inventory

    def cleanup_preview(self, environment_id: str) -> dict:
        preview = self.inventory.cleanup_preview(environment_id)
        if not preview.get("found"):
            return preview

        blockers = []
        for reason in preview.get("blockingReasons") or []:
            if "Publish" in reason:
                continue
            if reason == "Active Runner leases reference published releases.":
                continue
            blockers.append(reason)
        preview["blockingReasons"] = blockers

        environment = preview["environment"]
        env_key = environment["envKey"]
        image_ref = environment.get("imageRef")

        try:
            with self.inventory.connect("build") as conn:
                row = self.inventory._execute(
                    conn,
                    """
                    SELECT lifecycle_state, cleanup_error, retired_at
                    FROM build.runtime_environments
                    WHERE id = %s::uuid
                    """,
                    (environment_id,),
                ).fetchone()

            if row is None:
                preview["blockingReasons"].append(
                    "Build environment disappeared during lifecycle preflight."
                )
            else:
                environment["lifecycleState"] = row[0]
                environment["cleanupError"] = row[1]
                environment["retiredAt"] = (
                    row[2].isoformat()
                    if hasattr(row[2], "isoformat")
                    else row[2]
                )
        except Exception as exc:
            log.warning("Build lifecycle preflight unavailable (%s)", type(exc).__name__)
            preview["blockingReasons"].append(
                "Build lifecycle columns are unavailable; apply the lifecycle upgrade."
            )

        try:
            with self.inventory.connect("publish") as conn:
                rows = self.inventory._execute(
                    conn,
                    """
                    WITH latest_contracts AS (
                        SELECT operator_id, MAX(version) AS version
                        FROM publish.virtual_contract_versions
                        GROUP BY operator_id
                    )
                    SELECT
                        o.id::text,
                        o.name,
                        o.display_name,
                        v.id::text,
                        v.published_ref,
                        v.published_metadata
                    FROM publish.backend_variants v
                    JOIN publish.virtual_contract_versions c
                      ON c.id = v.contract_id
                    JOIN latest_contracts latest
                      ON latest.operator_id = c.operator_id
                     AND latest.version = c.version
                    JOIN publish.operators o
                      ON o.id = c.operator_id
                    WHERE v.backend = 'runner'
                      AND v.status = 'PUBLISHED'
                      AND v.published_metadata IS NOT NULL
                      AND (
                        v.published_metadata->>'runtimeEnvKey' = %s
                        OR v.published_metadata->>'envKey' = %s
                        OR (
                            %s IS NOT NULL
                            AND v.published_metadata->>'runtimeImage' = %s
                        )
                      )
                    ORDER BY o.display_name, v.updated_at DESC
                    LIMIT 101
                    """,
                    (env_key, env_key, image_ref, image_ref),
                ).fetchall()

            references = [
                {
                    "operatorId": row[0],
                    "name": row[1],
                    "displayName": row[2],
                    "variantId": row[3],
                    "publishedRef": row[4],
                    "publishedMetadata": row[5],
                }
                for row in rows[:100]
            ]
            preview["currentPublishedReferences"] = references
            preview["publishedReferences"] = len(references)

            if references:
                preview["blockingReasons"].append(
                    "Current published Runner variants still reference this runtime."
                )
            if len(rows) > 100:
                preview["blockingReasons"].append(
                    "Current Publish references exceed the lifecycle query limit."
                )
        except Exception as exc:
            log.warning("Current Publish reference check failed (%s)", type(exc).__name__)
            preview["currentPublishedReferences"] = []
            preview["blockingReasons"].append(
                "Cannot verify CURRENT Publish references; cleanup must not proceed."
            )

        if preview.get("activeLeaseReferences"):
            preview.setdefault("warnings", []).append(
                "Unexpired Runner leases exist. During execution the Runner "
                "RETIRING gate makes them non-executable before image deletion."
            )

        preview.setdefault("warnings", []).append(
            "Physical sandbox and in-flight acquisition safety is re-checked by "
            "Runner Engine during cleanup execution."
        )
        preview["canExecute"] = not preview["blockingReasons"]
        preview["action"] = "owner_lifecycle"
        return preview
