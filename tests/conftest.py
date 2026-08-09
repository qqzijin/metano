"""pytest configuration: auto-cleanup test data after each test."""

import pytest
from metano.evo_models import _get_conn


@pytest.fixture(autouse=True)
def _cleanup_test_proposals():
    yield
    conn = _get_conn()
    conn.execute("DELETE FROM proposals WHERE source='test'")
    conn.execute("DELETE FROM effect_baselines WHERE proposal_id NOT IN (SELECT id FROM proposals)")
    conn.commit()
    conn.close()
