"""Audit P1-5: cost pricing is unified on ``model_router.estimate_cost``.

The evolution / indexer pricing tables (``llm_call._COST_PER_MILLION``,
``indexer.MODEL_PRICING``) have been removed — the single authority is
``model_router.estimate_cost``. This pins the two audit artifacts:

- placeholder model ``<synthetic>`` prices at $0 (must never fall through to
  the DEFAULT sonnet rate that inflated it to $254.25);
- disabled provider ``gpt-5.6-luna`` prices at its configured 0.2 / 1.2 / 0.02
  per-million rates, not the 3 / 15 / 0.3 DEFAULT (which inflated it ~15x).

The ``luna_config`` fixture writes a gateway_config.yaml to the isolated
CONFIG_PATH and refreshes the router so the disabled provider's price table
entry is loaded exactly as in production.
"""

import pytest
import yaml

from metano import indexer
from metano import llm_call
from metano import model_router as mr

pytestmark = pytest.mark.usefixtures("isolated_env")


@pytest.fixture()
def luna_config():
    """Load the disabled ``gpt-5.6-luna`` provider into the isolated router."""
    cfg = {
        "models": {
            "gpt-5.6-luna": {
                "base_url": "https://opencode.ai/zen/go",
                "enabled": False,  # disabled providers must still be priced at config rates
                "model": "gpt-5.6-luna",
                "max_tokens": 4096,
                "price": {"input": 0.2, "output": 1.2, "cache_read": 0.02},
            },
        }
    }
    mr.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    mr.CONFIG_PATH.write_text(yaml.dump(cfg, allow_unicode=True))
    mr.model_router.refresh()
    return mr.model_router


def test_estimate_cost_synthetic_is_zero():
    # Placeholder model must never price at the DEFAULT sonnet rate.
    assert mr.ModelRouter.estimate_cost("<synthetic>", 1_000_000, 500_000, 100_000) == 0.0
    assert mr.ModelRouter.estimate_cost("<synthetic>", 0, 0, 0) == 0.0
    # Router already treats empty / test-y names as placeholders.
    assert mr.ModelRouter.estimate_cost("", 1000, 100) == 0.0


def test_estimate_cost_luna_uses_configured_price(luna_config):
    cost = mr.ModelRouter.estimate_cost("gpt-5.6-luna", 1000, 100, 1000)
    expected = (1000 * 0.2 + 100 * 1.2 + 1000 * 0.02) / 1_000_000
    assert cost == pytest.approx(expected, rel=1e-9)
    # Sanity: must NOT match the DEFAULT 3/15/0.3 rate.
    assert cost < (1000 * 3.0 + 100 * 15.0 + 1000 * 0.3) / 1_000_000


def test_llm_call_delegates_to_router_no_local_table():
    # The local price table is gone; _estimate_cost == router.estimate_cost.
    assert not hasattr(llm_call, "_COST_PER_MILLION")
    assert llm_call._estimate_cost("<synthetic>", 1000, 100, 100) == 0.0
    # Unknown model falls back to the router's DEFAULT rate.
    assert llm_call._estimate_cost("claude-sonnet-4-6", 1000, 100, 0) == pytest.approx(
        (1000 * 3.0 + 100 * 15.0) / 1_000_000
    )


def test_indexer_delegates_to_router_no_local_table(luna_config):
    assert not hasattr(indexer, "MODEL_PRICING")
    assert indexer.estimate_cost("<synthetic>", 1000, 100, 100) == 0.0
    assert indexer.estimate_cost("gpt-5.6-luna", 1000, 100, 1000) == pytest.approx(
        (1000 * 0.2 + 100 * 1.2 + 1000 * 0.02) / 1_000_000
    )
