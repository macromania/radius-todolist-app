import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
KEY = 'literal-$(id)-`id`-"quotes"-' + "x" * 40


@pytest.fixture
def checkout(tmp_path):
    for relative in ("scripts/lib/env.sh", "scripts/operations/init.sh"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    binary = tmp_path / "bin"
    binary.mkdir()
    az = binary / "az"
    az.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$AZ_CALLS"\nprintf \'%s\\n\' "$AZ_SUB"\n')
    az.chmod(0o700)
    return tmp_path


def run_init(root, *args, values=None):
    return subprocess.run(
        ["bash", str(root / "scripts/operations/init.sh"), *args],
        cwd=root,
        env={
            **os.environ,
            "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
            "AZ_CALLS": str(root / "az-calls"),
            "AZ_SUB": SUBSCRIPTION,
            **(values or {}),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def stored(root):
    return {
        key: json.loads(value)
        for key, value in (line.split("=", 1) for line in (root / ".env").read_text().splitlines())
    }


def test_local_shell_init_never_calls_azure_or_creates_state(checkout):
    result = run_init(checkout, "--environment", "local", values={"DEMO_ENV": "azure"})
    assert result.returncode == 0, result.stderr
    assert stored(checkout) == {
        "DEMO_ENV": "local",
        "DEMO_PROJECT": "radplanes",
        "DEMO_DEPLOYMENT": "learning",
    }
    assert (checkout / ".env").stat().st_mode & 0o777 == 0o600
    assert not (checkout / "az-calls").exists()
    assert not (checkout / ".state").exists()


def test_shell_replacement_switches_environment_and_removes_secrets(checkout):
    first = run_init(
        checkout,
        "--environment",
        "azure",
        "--demo-key-from-env",
        "management=SUPPLIED_KEY",
        values={"SUPPLIED_KEY": KEY},
    )
    assert first.returncode == 0, first.stderr
    assert stored(checkout)["AZURE_SUBSCRIPTION_ID"] == SUBSCRIPTION
    assert stored(checkout)["DEMO_KEY_MANAGEMENT"] == KEY
    assert KEY not in first.stdout + first.stderr
    assert (checkout / "az-calls").read_text().strip() == (
        "account show --query id --output tsv --only-show-errors"
    )
    second = run_init(checkout, "--environment", "local")
    assert second.returncode == 0, second.stderr
    assert not any(name.startswith(("AZURE_", "DEMO_KEY_")) for name in stored(checkout))
    before = (checkout / ".env").read_bytes()
    assert run_init(checkout, "--environment", "local").returncode == 0
    assert (checkout / ".env").read_bytes() == before


@pytest.mark.parametrize("value", ["short", "x" * 32 + "\n", "x" * 32 + " ", "ü" * 40])
def test_shell_refuses_invalid_http_credentials_without_replacing(checkout, value):
    assert run_init(checkout, "--environment", "local").returncode == 0
    before = (checkout / ".env").read_bytes()
    failed = run_init(
        checkout,
        "--environment",
        "local",
        "--demo-key-from-env",
        "shared-data=KEY_INPUT",
        values={"KEY_INPUT": value},
    )
    assert failed.returncode != 0
    assert value not in failed.stdout + failed.stderr
    assert (checkout / ".env").read_bytes() == before
    assert list(checkout.glob(".env.init.*")) == []


@pytest.mark.parametrize(
    "args",
    [
        ["--environment", "local", "--subscription", SUBSCRIPTION],
        ["--environment", "local", "--project", "../outside"],
        ["--environment", "local", "--deployment", "UpperCase"],
        ["--environment", "azure", "--subscription", "key not found"],
        ["--environment", "local", "--demo-key-from-env", "unknown=KEY_INPUT"],
    ],
)
def test_shell_invalid_input_preserves_previous_configuration(checkout, args):
    assert run_init(checkout, "--environment", "local").returncode == 0
    before = (checkout / ".env").read_bytes()
    assert run_init(checkout, *args).returncode != 0
    assert (checkout / ".env").read_bytes() == before


def test_shell_does_not_replace_a_symlink(checkout):
    other = checkout / "unrelated"
    other.write_text("preserve")
    (checkout / ".env").symlink_to(other)
    assert run_init(checkout, "--environment", "local").returncode != 0
    assert other.read_text() == "preserve"
    assert (checkout / ".env").is_symlink()


def test_failed_atomic_move_keeps_the_existing_file(checkout):
    assert run_init(checkout, "--environment", "local").returncode == 0
    original = (checkout / ".env").read_bytes()
    command = checkout / "bin/mv"
    command.write_text("#!/bin/sh\necho 'synthetic replacement failure' >&2\nexit 1\n")
    command.chmod(0o700)
    result = run_init(checkout, "--environment", "local", "--deployment", "second")
    assert result.returncode != 0
    assert "Configured" not in result.stdout
    assert (checkout / ".env").read_bytes() == original
    assert list(checkout.glob(".env.init.*")) == []


def test_shell_output_is_readable_by_typed_runtime_configuration(checkout):
    assert (
        run_init(
            checkout,
            "--environment",
            "local",
            "--demo-key-from-env",
            "management=SUPPLIED_KEY",
            values={"SUPPLIED_KEY": KEY},
        ).returncode
        == 0
    )
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            "-c",
            "import os; from pathlib import Path; "
            "from scripts.operations.config import load_config; "
            "c=load_config(Path(os.environ['CONFIG_FILE'])); "
            "assert c.environment=='local'; "
            "assert c.demo_keys['management']==os.environ['EXPECTED']; "
            "print(c.stem)",
        ],
        cwd=ROOT,
        env={**os.environ, "CONFIG_FILE": str(checkout / ".env"), "EXPECTED": KEY},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "radplanes-learning-local"


@pytest.mark.parametrize(
    "extra",
    [
        "UNKNOWN=secret",
        "DEMO_ENV=azure",
        "export DEMO_PROJECT=example",
        'AZURE_LOCATION=""',
        'DEMO_KEY_VAULT=""',
        'DEMO_REVISION=""',
        'DEMO_PROJECT="demo" trailing',
        'DEMO_KEY_MANAGEMENT="x\\n' + "x" * 32 + '"',
    ],
)
def test_shell_loader_rejects_unknown_duplicate_executable_or_invalid_keys(checkout, extra):
    assert run_init(checkout, "--environment", "local").returncode == 0
    with (checkout / ".env").open("a") as stream:
        stream.write(extra + "\n")
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; demo_load_env "$2"',
            "check",
            str(checkout / "scripts/lib/env.sh"),
            str(checkout / ".env"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "secret" not in result.stdout
