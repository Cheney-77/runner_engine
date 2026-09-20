from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .harbor import HarborClient, HarborError


ACTIVE = "ACTIVE"
RETIRING = "RETIRING"
RETIRED = "RETIRED"


@dataclass(frozen=True)
class BuildLifecyclePlan:
    environment_id: str
    env_key: str
    build_status: str
    lifecycle_state: str
    image_ref: str | None
    alias_count: int
    active_build_jobs: int

    @property
    def locally_blocked(self) -> bool:
        return self.active_build_jobs > 0 or self.build_status in {
            "PENDING",
            "BUILDING",
            "VERIFYING",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "environmentId": self.environment_id,
            "envKey": self.env_key,
            "buildStatus": self.build_status,
            "lifecycleState": self.lifecycle_state,
            "imageRef": self.image_ref,
            "aliasCount": self.alias_count,
            "activeBuildJobs": self.active_build_jobs,
            "locallyBlocked": self.locally_blocked,
        }


class BuildLifecycle:
    def __init__(self, store, harbor: HarborClient | None):
        self.store = store
        self.harbor = harbor

    def plan(self, environment_id: str) -> BuildLifecyclePlan:
        row = self.store.get(environment_id)
        if row is None:
            raise KeyError(environment_id)

        return BuildLifecyclePlan(
            environment_id=str(row["id"]),
            env_key=row["env_key"],
            build_status=row["status"],
            lifecycle_state=row.get("lifecycle_state", ACTIVE),
            image_ref=row["image_ref"],
            alias_count=self.store.count_aliases(row["id"]),
            active_build_jobs=self.store.count_active_build_jobs(row["id"]),
        )

    def begin_retirement(self, environment_id: str, *, reason: str) -> dict:
        plan = self.plan(environment_id)
        if plan.locally_blocked:
            raise ValueError(
                "runtime environment has active build work or is still being prepared"
            )
        if plan.lifecycle_state in {RETIRING, RETIRED}:
            current = self.store.get(environment_id)
            if current is None:
                raise KeyError(environment_id)
            return dict(current)

        row = self.store.begin_retirement(environment_id, reason=reason)
        if row is None:
            raise ValueError("runtime environment could not enter RETIRING")
        return dict(row)

    def cancel_retirement(self, environment_id: str) -> dict:
        row = self.store.cancel_retirement(environment_id)
        if row is None:
            current = self.store.get(environment_id)
            if current is None:
                raise KeyError(environment_id)
            raise ValueError("only RETIRING environments can return to ACTIVE")
        return dict(row)

    def delete_artifact(
        self,
        environment_id: str,
        *,
        expected_image_ref: str,
    ) -> dict:
        plan = self.plan(environment_id)
        if plan.lifecycle_state != RETIRING:
            raise ValueError("runtime environment must be RETIRING before deletion")
        if plan.locally_blocked:
            raise ValueError("runtime environment still has active build work")
        if plan.image_ref != expected_image_ref:
            raise ValueError("image_ref changed after cleanup planning")

        if not plan.image_ref:
            self.store.finalize_retirement(environment_id)
            return {
                "environmentId": environment_id,
                "imageRef": None,
                "artifactDeleted": False,
                "alreadyMissing": True,
            }

        if self.harbor is None:
            raise RuntimeError(
                "Harbor deletion is disabled; configure BUILD_HARBOR_URL and credentials"
            )

        try:
            result = self.harbor.delete_artifact(plan.image_ref)
        except HarborError as exc:
            self.store.record_cleanup_error(environment_id, str(exc))
            raise

        self.store.finalize_retirement(environment_id)
        return {
            "environmentId": environment_id,
            "imageRef": plan.image_ref,
            "artifactDeleted": bool(result["deleted"]),
            "alreadyMissing": bool(result["alreadyMissing"]),
            "harbor": result,
            "garbageCollectionRequired": True,
        }
