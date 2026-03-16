from __future__ import annotations

import pytest

from onclaw.config import OnclawConfig


@pytest.fixture
def sample_config() -> OnclawConfig:
    return OnclawConfig(
        slack_app_token="xapp-test-token",
        slack_bot_token="xoxb-test-token",
        anthropic_api_key="sk-ant-test-key",
        kubeconfig_path="/tmp/fake-kubeconfig",
        claude_model="claude-sonnet-4-20250514",
        claude_max_tokens=1024,
    )
