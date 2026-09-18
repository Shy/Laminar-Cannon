"""Temporal Cloud connection shared by both workers and the schedule tool.

Credentials come from the environment, optionally seeded from a ``.env`` file
so the Pi and the MacBook are configured identically. API key authentication
is used rather than mTLS: there are no certificates to distribute to the Pi,
which matters because its SD card gets reimaged.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from temporalio.client import Client

#: Environment variables required to reach Temporal Cloud.
ADDRESS_VAR = "TEMPORAL_ADDRESS"
NAMESPACE_VAR = "TEMPORAL_NAMESPACE"
API_KEY_VAR = "TEMPORAL_API_KEY"


@dataclass(frozen=True)
class CloudConfig:
    """Resolved Temporal Cloud connection settings.

    :param address: ``<namespace>.tmprl.cloud:7233`` endpoint.
    :param namespace: Fully qualified namespace, ``<name>.<account>``.
    :param api_key: Cloud API key.
    """

    address: str
    namespace: str
    api_key: str

    def describe(self) -> str:
        """Summarise the connection without revealing the API key.

        :returns: Human-readable summary safe to print in logs.
        """
        return f"{self.namespace} at {self.address} (api key auth)"


def load_config(env_file: Path | None = None) -> CloudConfig:
    """Read connection settings from the environment.

    :param env_file: Optional ``.env`` to load first. Existing environment
        variables win, so an explicit export overrides the file.
    :returns: Resolved configuration.
    :raises SystemExit: If any required variable is missing, naming all of
        them at once rather than failing one at a time.
    """
    candidate = env_file or Path(__file__).resolve().parents[2] / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=False)

    missing = [
        name
        for name in (ADDRESS_VAR, NAMESPACE_VAR, API_KEY_VAR)
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}. Copy .env.example to .env and fill "
            "it in, or export the variables."
        )

    return CloudConfig(
        address=os.environ[ADDRESS_VAR],
        namespace=os.environ[NAMESPACE_VAR],
        api_key=os.environ[API_KEY_VAR],
    )


async def connect(config: CloudConfig | None = None) -> Client:
    """Connect to Temporal Cloud.

    :param config: Connection settings; loaded from the environment if omitted.
    :returns: A connected client.
    """
    resolved = config or load_config()
    return await Client.connect(
        resolved.address,
        namespace=resolved.namespace,
        api_key=resolved.api_key,
        tls=True,
    )
