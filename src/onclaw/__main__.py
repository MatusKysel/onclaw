from __future__ import annotations

import argparse
import logging
import signal
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Onclaw - Kubernetes alert investigation bot"
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to config file (optional — env vars are enough)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Suppress noisy library loggers
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)

    from onclaw.app import create_app

    try:
        app = create_app(args.config)
    except Exception as e:
        logging.getLogger(__name__).error("Failed to start: %s", e)
        sys.exit(1)

    # Handle graceful shutdown
    def handle_signal(signum: int, frame: object) -> None:
        logging.getLogger(__name__).info("Received signal %d, shutting down...", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    app.start()


if __name__ == "__main__":
    main()
