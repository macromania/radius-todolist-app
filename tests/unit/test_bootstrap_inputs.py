from pathlib import Path

import pytest

from plane_demo.bootstrap import initialize


def test_management_reporting_login_cannot_alias_privileged_runtime_role():
    with pytest.raises(ValueError, match="child-only"):
        initialize(
            "must-not-connect",
            "management",
            {"mgmt_api": "a" * 32, "mgmt_provisioner": "b" * 32},
            [{"pair_id": "shared", "reporting_role": "mgmt_api"}],
            Path("sql"),
        )


def test_initializer_rejects_weak_password_before_connecting():
    with pytest.raises(ValueError, match="password shorter"):
        initialize(
            "must-not-connect",
            "control",
            {"cp_api": "weak", "cp_reconciler": "b" * 32, "dp_reconciler": "c" * 32},
            [],
            Path("sql"),
            "shared",
        )
