from __future__ import annotations

import logging
import threading
from pathlib import Path

from onclaw.ai_summarizer import AISummarizer
from onclaw.config import OnclawConfig, load_config
from onclaw.investigation import InvestigationOrchestrator
from onclaw.k8s_investigator import K8sInvestigator
from onclaw.memory import Memory

logger = logging.getLogger(__name__)


class Onclaw:
    """Top-level application that runs all configured platform listeners."""

    def __init__(self, listeners: list) -> None:
        self._listeners = listeners

    def start(self) -> None:
        if not self._listeners:
            raise RuntimeError("No listeners configured")

        if len(self._listeners) == 1:
            self._listeners[0].start()
        else:
            logger.info("Starting %d platform listeners...", len(self._listeners))
            threads = []
            for listener in self._listeners:
                t = threading.Thread(target=listener.start, daemon=True)
                t.start()
                threads.append(t)
            # Block until all threads finish (or interrupted)
            for t in threads:
                t.join()


def create_app(config_path: str | Path | None = None) -> Onclaw:
    """Create and wire all application components."""
    config = load_config(config_path)
    logger.info("Configuration loaded")

    # Shared: K8s discovery
    k8s = K8sInvestigator(config.kubeconfig_path)
    cluster_info = k8s.discover_cluster_info()
    for ctx_name, namespaces in cluster_info.contexts.items():
        logger.info("K8s context '%s': %d namespaces", ctx_name, len(namespaces))

    # Shared: AI summarizer
    summarizer = AISummarizer(
        api_key=config.anthropic_api_key,
        model=config.claude_model,
        fast_model=config.claude_fast_model,
        max_tokens=config.claude_max_tokens,
    )

    # Shared: memory
    memory = Memory(config.memory_path)
    logger.info("Investigation memory initialized at %s", config.memory_path)

    # Shared: orchestrator (platform-agnostic)
    orchestrator = InvestigationOrchestrator(
        config=config,
        k8s=k8s,
        summarizer=summarizer,
        memory=memory,
    )

    listeners: list = []

    # Slack
    if config.slack_bot_token and config.slack_app_token:
        from slack_sdk import WebClient

        from onclaw.notifier import SlackNotifier
        from onclaw.slack_listener import SlackListener

        slack_client = WebClient(token=config.slack_bot_token)
        notifier = SlackNotifier(slack_client)
        listeners.append(SlackListener(
            config=config,
            orchestrator=orchestrator,
            summarizer=summarizer,
            cluster_info=cluster_info,
            notifier=notifier,
        ))
        logger.info("Slack listener configured")

    # Telegram
    if config.telegram_bot_token:
        from onclaw.telegram_listener import TelegramListener

        listeners.append(TelegramListener(
            config=config,
            orchestrator=orchestrator,
            summarizer=summarizer,
            cluster_info=cluster_info,
        ))

    if not listeners:
        raise RuntimeError(
            "No platform configured. Set SLACK_APP_TOKEN + SLACK_BOT_TOKEN for Slack, "
            "and/or TELEGRAM_BOT_TOKEN for Telegram."
        )

    return Onclaw(listeners)
