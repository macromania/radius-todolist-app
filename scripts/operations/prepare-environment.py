#!/usr/bin/env python3
"""Prepare or retire one environment from its owned administrative Job."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from environment_job import environment_guards  # noqa: E402

from plane_demo.management.providers.credentials import StoredCredentials  # noqa: E402
from plane_demo.management.providers.environment_registry import EnvironmentRegistry  # noqa: E402
from plane_demo.management.provisioner import service_provider  # noqa: E402
from plane_demo.management.provisioning import (  # noqa: E402
    OperatorConfig,
    ProvisioningError,
    prepare_pair,
)
from plane_demo.shared.db import ProvisionerAlreadyRunning, provisioner_session  # noqa: E402


def execute_environment(provider, configuration, pair, retire, guard):
    registry = EnvironmentRegistry(configuration, provider.credentials.administrator_dsn(), guard)
    registry.observe()
    if retire:
        registry.retire(pair)
        print(json.dumps({"pair_id": pair, "status": "retired"}))
        return 0
    password = provider.credentials.reporting_password(pair)
    registry.register(pair, password)
    del password
    provider.credentials.seed_provided_keys({f"{pair}-control", f"{pair}-data"})
    result = prepare_pair(
        pair,
        provider,
        lambda stage: logging.info("environment=%s stage=%s", pair, stage),
    )
    provider.inspect_pair(pair)
    provider.kubectl(
        f"{pair}-data",
        "-n",
        configuration.namespace(f"{pair}-data"),
        "exec",
        "deployment/data-api",
        "--",
        "python",
        "-c",
        "from plane_demo.shared.settings import Settings,redis_client; "
        "client=redis_client(Settings.from_env('data_api')); "
        "result=client.ping(); client.close(); "
        "print('redis_ready' if result is True else 'redis_unavailable'); "
        "raise SystemExit(0 if result is True else 1)",
    )
    registry.available(pair)
    print(
        json.dumps(
            {
                "pair_id": pair,
                "status": "available",
                "control_url": result.control_url,
                "data_url": result.data_url,
            }
        )
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--retire", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    configuration = OperatorConfig.load(args.config)
    if args.pair not in {item["pair_id"] for item in configuration.pair_slots}:
        raise ProvisioningError("invalid_pair_assignment")
    with environment_guards(configuration) as (active, writer):
        operations = None

        def guard():
            active()
            if operations is not None:
                operations.connection.execute("SELECT 1").fetchone()

        def guarded_writer():
            writer()
            if operations is None:
                raise ProvisioningError("environment_database_lock_required")
            guard()

        with service_provider(
            configuration, ROOT, guard=guard, writer_guard=guarded_writer
        ) as provider:
            provider.authenticate(workload_required=True)
            provider.get_access("management")
            provider.seed_management_workspace()
            if not isinstance(provider.credentials, StoredCredentials):
                raise ProvisioningError("service_credentials_required")
            with provisioner_session(
                provider.credentials.dsn("management", "mgmt_provisioner")
            ) as held:
                operations = held
                if held.connection.execute(
                    "SELECT 1 FROM management.operations "
                    "WHERE status IN ('pending','running') LIMIT 1"
                ).fetchone():
                    raise ProvisioningError("unexpected_tenant_infrastructure_operation")
                return execute_environment(provider, configuration, args.pair, args.retire, guard)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ProvisioningError,
        ProvisionerAlreadyRunning,
        psycopg.Error,
        ValueError,
        KeyError,
        OSError,
    ) as error:
        logging.error(
            "environment_preparation_failed code=%s",
            error.code if isinstance(error, ProvisioningError) else type(error).__name__,
        )
        raise SystemExit(1) from None
