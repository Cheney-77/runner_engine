import pytest
from runner_engine.errors import QuotaError
from runner_engine.quota import Quota


def test_live_quota_is_not_released_when_creation_finishes():
    quota = Quota(max_creating=1, max_live=1, max_live_per_tenant=1)
    quota.reserve_live("a")
    with quota.creation_slot():
        pass
    assert quota.live == 1
    with pytest.raises(QuotaError):
        quota.reserve_live("a")
    quota.release_live("a")
    assert quota.live == 0
