import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from plane_demo.setup import bootstrap

ROLES = {"cp_api", "cp_reconciler", "dp_reconciler"}
PASSWORDS = {role: "synthetic-only-" + role + "x" * 40 for role in ROLES}
CATALOG = {"policies": ["owned policy"], "roles": ["expected privileges"]}


class Connection:
    def __init__(self, expected, *, existing=False, changed=False):
        self.expected = expected
        self.existing = existing
        self.changed = changed
        self.calls = []
        self.completed = False

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.completed = kind is None

    def execute(self, statement, parameters=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        self.calls.append((text, parameters))
        value = None
        rows = []
        if text == "SELECT to_regclass('demo_metadata.schema_version')":
            value = "demo_metadata.schema_version" if self.existing else None
        elif text == "SELECT to_regnamespace(%s)":
            value = None
        elif text.startswith("SELECT schema_kind,version,configuration"):
            rows = [
                (
                    "control",
                    bootstrap.SCHEMA_VERSION,
                    self.expected["configuration"],
                    self.expected["schema_sha256"],
                    digest(CATALOG),
                )
            ]
        elif text == bootstrap.CATALOG_QUERY:
            value = {"policies": ["changed"]} if self.changed else CATALOG
        elif text == "SELECT session_user":
            value = "setup"
        elif text.startswith("SELECT rolsuper FROM pg_roles WHERE rolname=session_user"):
            value = True
        if text.startswith(("SELECT rolsuper, rolbypassrls", "SELECT m.inherit_option")):
            row = None
        else:
            row = (value,)
        return Mock(fetchone=lambda: row, fetchall=lambda: rows)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.fixture
def expected():
    return {
        "configuration": {"pair_id": "shared", "slots": []},
        "schema_sha256": hashlib.sha256(Path("sql/control.sql").read_bytes()).hexdigest(),
    }


def test_initializer_writes_version_in_schema_transaction(monkeypatch, expected):
    connection = Connection(expected)
    monkeypatch.setattr(bootstrap.psycopg, "connect", lambda *a, **kw: connection)
    bootstrap.initialize("synthetic-not-used", "control", PASSWORDS, [], Path("sql"), "shared")
    assert connection.completed
    calls = [call for call, _ in connection.calls]
    assert calls.index(Path("sql/control.sql").read_text()) < next(
        i
        for i, call in enumerate(calls)
        if call.startswith("INSERT INTO demo_metadata.schema_version")
    )
    marker = next(
        args
        for call, args in connection.calls
        if call.startswith("INSERT INTO demo_metadata.schema_version")
    )
    assert marker[:2] == ("control", bootstrap.SCHEMA_VERSION)
    assert json.loads(marker[2]) == expected["configuration"]
    assert marker[3:5] == (expected["schema_sha256"], digest(CATALOG))
    assert not any("COMMIT" == call for call in calls)


def test_lost_acknowledgement_is_observed_without_ddl_or_password_rotation(monkeypatch, expected):
    connection = Connection(expected, existing=True)
    monkeypatch.setattr(bootstrap.psycopg, "connect", lambda *a, **kw: connection)
    bootstrap.initialize("synthetic-not-used", "control", PASSWORDS, [], Path("sql"), "shared")
    assert connection.completed
    assert all(call.lstrip().startswith("SELECT") for call, _ in connection.calls)
    assert not any("ALTER ROLE" in call for call, _ in connection.calls)


def test_changed_security_catalog_blocks_reinitialization(monkeypatch, expected):
    connection = Connection(expected, existing=True, changed=True)
    monkeypatch.setattr(bootstrap.psycopg, "connect", lambda *a, **kw: connection)
    with pytest.raises(ValueError, match="schema_contract_changed"):
        bootstrap.initialize("synthetic-not-used", "control", PASSWORDS, [], Path("sql"), "shared")
    assert not connection.completed
    assert all(call.lstrip().startswith("SELECT") for call, _ in connection.calls)


def test_existing_schema_without_marker_is_not_adopted(expected):
    connection = Mock()
    connection.execute.side_effect = [
        Mock(fetchone=lambda: (None,)),
        Mock(fetchone=lambda: ("control",)),
    ]
    with pytest.raises(ValueError, match="schema_version_missing"):
        bootstrap.initialized(connection, "control", ROLES, expected)


def test_source_or_configuration_mismatch_stops_before_catalog_query(expected):
    connection = Connection(expected, existing=True)
    other = {**expected, "configuration": {"pair_id": "different", "slots": []}}
    with pytest.raises(ValueError, match="schema_version_mismatch"):
        bootstrap.initialized(connection, "control", ROLES, other)
    assert not any(call == bootstrap.CATALOG_QUERY for call, _ in connection.calls)
