import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.operations import config, demo  # noqa: E402

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
KEY = 'synthetic-credential-$(`never-execute`)-"quoted"-' + "x" * 20


def local(**kwargs):
    return config.DemoConfig("local", "radplanes", "learning", **kwargs)


def test_local_init_uses_no_cloud_calls_or_inherited_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    cloud = Mock(side_effect=AssertionError("local initialization contacted cloud"))
    monkeypatch.setattr(demo.subprocess, "run", cloud)
    monkeypatch.setenv("DEMO_ENV", "azure")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "must-not-be-used")
    assert demo.main(["init", "--environment", "local"]) == 0
    path = tmp_path / ".env"
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.load_config(path) == local()
    assert demo.main(["config"]) == 0
    assert "must-not-be-used" not in capsys.readouterr().out
    cloud.assert_not_called()
    assert not (tmp_path / ".state").exists()


def test_init_replaces_environment_and_old_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    monkeypatch.setenv("SYNTHETIC_KEY", KEY)
    assert (
        demo.main(
            [
                "init",
                "--environment",
                "azure",
                "--subscription",
                SUBSCRIPTION,
                "--demo-key-from-env",
                "management=SYNTHETIC_KEY",
            ]
        )
        == 0
    )
    previous = config.load_config(tmp_path / ".env")
    assert previous.subscription == SUBSCRIPTION and previous.demo_keys["management"] == KEY
    assert demo.main(["init", "--environment", "local"]) == 0
    current = config.load_config(tmp_path / ".env")
    assert current.subscription is None and not current.demo_keys
    assert "AZURE_" not in (tmp_path / ".env").read_text()
    before = (tmp_path / ".env").read_bytes()
    assert demo.main(["init", "--environment", "local"]) == 0
    assert (tmp_path / ".env").read_bytes() == before


def test_credentials_round_trip_as_literal_and_remain_redacted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setenv("SYNTHETIC_KEY", KEY)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    assert (
        demo.main(
            [
                "init",
                "--environment",
                "local",
                "--demo-key-from-env",
                "shared-data=SYNTHETIC_KEY",
            ]
        )
        == 0
    )
    stored = config.load_config(tmp_path / ".env")
    assert stored.demo_keys["shared-data"] == KEY
    assert KEY not in repr(stored)
    assert KEY not in json.dumps(stored.values())
    assert demo.main(["config"]) == 0
    assert KEY not in capsys.readouterr().out
    assert set(tmp_path.iterdir()) == {tmp_path / ".env"}
    with pytest.raises(TypeError):
        stored.demo_keys["management"] = KEY


def test_explicit_azure_inputs_do_not_require_cli_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    command = Mock(side_effect=AssertionError("unexpected Azure discovery"))
    monkeypatch.setattr(demo.subprocess, "run", command)
    assert (
        demo.main(
            [
                "init",
                "--environment",
                "azure",
                "--project",
                "learn",
                "--deployment",
                "team",
                "--subscription",
                SUBSCRIPTION,
                "--location",
                "westus3",
            ]
        )
        == 0
    )
    value = config.load_config(tmp_path / ".env")
    assert value.stem == "learn-team-azure"
    command.assert_not_called()


def test_subscription_suggestion_uses_read_only_account_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(SUBSCRIPTION), ""))
    monkeypatch.setattr(demo.subprocess, "run", run)
    assert demo.main(["init", "--environment", "azure"]) == 0
    assert run.call_args.args[0][:3] == ["az", "account", "show"]
    assert run.call_count == 1
    assert config.load_config(tmp_path / ".env").subscription == SUBSCRIPTION


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "sensitive-response", "sensitive-response"),
        subprocess.CompletedProcess([], 0, "sensitive-response", ""),
        subprocess.CompletedProcess([], 0, "{}", ""),
    ],
)
def test_bad_subscription_capture_never_replaces_configuration(
    tmp_path, monkeypatch, capsys, result
):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    config.initialize_config(local(), tmp_path / ".env")
    original = (tmp_path / ".env").read_bytes()
    monkeypatch.setattr(demo.subprocess, "run", Mock(return_value=result))
    assert demo.main(["init", "--environment", "azure"]) == 1
    assert (tmp_path / ".env").read_bytes() == original
    assert "sensitive-response" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "line",
    [
        "export DEMO_PROJECT=example",
        "DEMO_ENV=local\nDEMO_ENV=azure",
        "UNKNOWN_SECRET=hidden",
        'DEMO_PROJECT="bad" trailing',
        "DEMO_PROJECT='unclosed",
        "AZURE_SUBSCRIPTION_ID=secret",
        "DEMO_KEY_MANAGEMENT=short",
        "DEMO_DEPLOYMENT=../unsafe",
    ],
)
def test_invalid_file_values_fail_without_disclosure(line):
    with pytest.raises(config.ConfigError) as error:
        config.parse_env("DEMO_ENV=local\nDEMO_PROJECT=demo\nDEMO_DEPLOYMENT=team\n" + line)
    assert "hidden" not in str(error.value)


@pytest.mark.parametrize("syntax", ['"{}"', "'{}'", "{}"])
def test_dotenv_values_are_not_shell_interpolated(syntax):
    key = "$(id)" + "x" * 32
    text = (
        "DEMO_ENV=local\nDEMO_PROJECT=demo\nDEMO_DEPLOYMENT=team\n"
        + "DEMO_KEY_MANAGEMENT="
        + syntax.format(key)
    )
    assert config.parse_env(text).demo_keys["management"] == key


@pytest.mark.parametrize(
    "key",
    ["x" * 31, "x" * 513, KEY + "\u00e9", KEY + "\r\n", KEY + " ", KEY + "\t", KEY + "\x7f"],
)
def test_invalid_header_demo_key_preserves_configuration(tmp_path, monkeypatch, capsys, key):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    monkeypatch.setenv("SYNTHETIC_KEY", key)
    path = tmp_path / ".env"
    config.initialize_config(local(), path)
    original = path.read_bytes()
    with pytest.raises(config.ConfigError, match="ASCII"):
        config.parse_env(path.read_text() + f"DEMO_KEY_MANAGEMENT={json.dumps(key)}\n")
    assert (
        demo.main(
            [
                "init",
                "--environment",
                "local",
                "--demo-key-from-env",
                "management=SYNTHETIC_KEY",
            ]
        )
        == 1
    )
    assert path.read_bytes() == original
    output = capsys.readouterr()
    assert "ASCII" in output.err
    assert key not in output.out + output.err


@pytest.mark.parametrize(
    "key", ["!" * 32, "~" * 512, "".join(chr(code) for code in range(33, 127))]
)
def test_demo_key_ascii_range_and_length_boundaries_round_trip(tmp_path, key):
    value = local(demo_keys={"management": key})
    path = tmp_path / ".env"
    config.initialize_config(value, path)
    assert config.load_config(path).demo_keys["management"] == key


def test_missing_or_public_or_symlink_file_is_rejected(tmp_path):
    path = tmp_path / ".env"
    with pytest.raises(config.ConfigError, match="make init"):
        config.load_config(path)
    path.write_text("DEMO_ENV=local")
    path.chmod(0o644)
    with pytest.raises(config.ConfigError, match="private"):
        config.load_config(path)
    with pytest.raises(config.ConfigError, match="private"):
        config.initialize_config(local(), path)
    path.unlink()
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    path.symlink_to(target)
    with pytest.raises(config.ConfigError):
        config.initialize_config(local(), path)
    with pytest.raises(config.ConfigError):
        config.load_config(path)
    assert target.read_text() == "preserve"


def test_failed_replace_preserves_file_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    config.initialize_config(local(), path)
    before = path.read_bytes()
    monkeypatch.setattr(config.os, "replace", Mock(side_effect=OSError("replace failed")))
    with pytest.raises(OSError):
        config.initialize_config(config.DemoConfig("local", "demo", "second"), path)
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == {path}


def test_invalid_init_does_not_replace_valid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo.sys, "stdin", io.StringIO())
    config.initialize_config(local(), tmp_path / ".env")
    before = (tmp_path / ".env").read_bytes()
    assert demo.main(["init", "--environment", "local", "--subscription", SUBSCRIPTION]) == 1
    assert (tmp_path / ".env").read_bytes() == before


def test_physical_names_include_deployment_identity():
    first = config.DemoConfig("azure", "demo", "first", SUBSCRIPTION, "centralus")
    second = config.DemoConfig("azure", "demo", "second", SUBSCRIPTION, "centralus")
    assert first.registry_name != second.registry_name
    assert first.vault_name != second.vault_name
    assert first.slot_name("shared-control") != second.slot_name("shared-control")
    assert all(len(first.namespace(slot)) <= 63 for slot in config.SLOTS)
    assert len(first.vault_name) <= 24
    with pytest.raises(config.ConfigError):
        first.namespace("foreign")


def test_git_and_docker_exclude_actual_dotenv(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env", ".env.production"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [".env", ".env.production"]
    docker = (root / ".dockerignore").read_text().splitlines()
    assert "**/.env" in docker and "**/.env.*" in docker
    assert "!.env.example" in (root / ".gitignore").read_text()


def test_script_help_does_not_create_configuration(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [str(root / ".venv/bin/python"), str(root / "scripts/operations/demo.py"), "--help"],
        cwd=tmp_path,
        env={**os.environ, "DEMO_ENV": "invalid"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "{init,config}" in result.stdout
    assert not (tmp_path / ".env").exists()


def test_make_init_and_show_config_use_the_selected_checkout(tmp_path):
    root = Path(__file__).resolve().parents[2]
    scripts = tmp_path / "scripts/operations"
    scripts.mkdir(parents=True)
    for relative in (
        "Makefile",
        "scripts/__init__.py",
        "scripts/operations/__init__.py",
        "scripts/operations/config.py",
        "scripts/operations/demo.py",
        "scripts/operations/init.sh",
        "scripts/lib/output.sh",
        "scripts/lib/env.sh",
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, tmp_path / relative)
    environment = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "DEMO_ENV": "azure",
        "SYNTHETIC_KEY": KEY,
    }

    def make(*args):
        return subprocess.run(
            ["make", "--no-print-directory", "RUN=env", *args],
            cwd=tmp_path,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

    help_text = make("help").stdout
    assert "init" in help_text and "show-config" in help_text
    assert not (tmp_path / ".env").exists()
    initialized = make(
        "init",
        "ENV=local",
        "ARGS=--project demo --deployment team --demo-key-from-env management=SYNTHETIC_KEY",
    )
    stored = config.load_config(tmp_path / ".env")
    assert stored.project == "demo" and stored.deployment == "team"
    assert stored.environment == "local" and stored.demo_keys["management"] == KEY
    shown = make("show-config", "ENV=azure")
    assert '"DEMO_ENV": "local"' in shown.stdout
    assert "[redacted]" in shown.stdout
    assert KEY not in initialized.stdout + initialized.stderr + shown.stdout + shown.stderr
    assert not (tmp_path / ".state").exists()
