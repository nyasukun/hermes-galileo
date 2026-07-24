from __future__ import annotations

import pytest

from tests.e2e.test_live_galileo import _conversation_quality_value


@pytest.mark.parametrize(
    "payload",
    [
        {"conversation_quality": {"status_type": "pending"}},
        {
            "metric_info": {
                "conversation_quality": {
                    "status_type": "computing",
                    "message": "Metric is computing.",
                }
            }
        },
        {
            "metrics": [
                {
                    "name": "Conversation Quality",
                    "status_type": "failed",
                    "message": "Metric failed to compute.",
                }
            ]
        },
        {
            "conversation_quality": {
                "status_type": "not_computed",
                "value": 0.9,
            }
        },
    ],
)
def test_conversation_quality_rejects_incomplete_or_failed_metrics(
    payload: dict[str, object],
) -> None:
    assert _conversation_quality_value(payload) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"conversation_quality": 0.75}, 0.75),
        (
            {
                "metric_info": {
                    "conversation_quality": {
                        "status_type": "success",
                        "value": 0.8,
                    }
                }
            },
            0.8,
        ),
        (
            {
                "metrics": [
                    {
                        "metric_name": "Conversation Quality",
                        "status_type": "roll_up",
                        "score": 0.85,
                    }
                ]
            },
            0.85,
        ),
    ],
)
def test_conversation_quality_accepts_completed_scores(
    payload: dict[str, object],
    expected: float,
) -> None:
    assert _conversation_quality_value(payload) == expected
