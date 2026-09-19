import errno
import fcntl
import os
import pty
import re
import shutil
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECTIONS = {
    "setup": ["Setup and API"],
    "checks": ["Checks and tests"],
    "local": [
        "Local: preparation and images",
        "Local: deployment and acceptance",
        "Local: cleanup",
    ],
    "azure": [
        "Azure-first workflow: selected environment stages",
        "Azure: acceptance",
        "Selected environment: cleanup",
    ],
}
COMMAND = re.compile(r"^  ([a-z][a-z0-9_-]*) {2,}(\S.*)$", re.MULTILINE)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RULE = "-" * 44


def read_terminal(descriptor):
    chunks = []
    while True:
        try:
            chunk = os.read(descriptor, 4096)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode().replace("\r\n", "\n")


def run_make(directory, *args, environment=None, terminal=None, columns=None):
    options = {
        "args": ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), *args],
        "cwd": directory,
        "env": {
            **os.environ,
            "MAKEFLAGS": "",
            "MFLAGS": "",
            "MAKELEVEL": "0",
            "COLOR": "auto",
            "NO_COLOR": "",
            "TERM": "xterm-256color",
            **(environment or {}),
        },
        "text": True,
        "check": False,
        "timeout": 30,
    }
    if terminal is None:
        return subprocess.run(**options, capture_output=True)
    if terminal not in ("stdout", "stderr"):
        raise ValueError(f"Unknown terminal stream: {terminal}")
    master, slave = pty.openpty()
    try:
        if columns is not None:
            import termios

            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        with ThreadPoolExecutor(max_workers=1) as executor:
            captured = executor.submit(read_terminal, master)
            try:
                result = subprocess.run(
                    **options,
                    stdout=slave if terminal == "stdout" else subprocess.PIPE,
                    stderr=slave if terminal == "stderr" else subprocess.PIPE,
                )
            finally:
                os.close(slave)
            setattr(result, terminal, captured.result(timeout=5))
            return result
    finally:
        os.close(master)


def public_targets():
    targets = {}
    group = None
    for line in (ROOT / "Makefile").read_text().splitlines():
        if line.startswith("##@ "):
            group = line.split()[1]
        elif re.match(r"^[a-zA-Z0-9_-]+:.*## ", line):
            assert group is not None, f"Public target needs a help section: {line}"
            targets[line.split(":")[0]] = group
    return targets


@pytest.mark.parametrize("arguments", [[], ["help"], ["--silent", "help"]])
def test_default_help_is_grouped_complete_and_side_effect_free(tmp_path, arguments):
    result = run_make(tmp_path, *arguments, "GROUP=all", "ENV=invalid", "RUN=false")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.startswith(f"\n\nRadius three-plane demo\n{RULE}\n\n")
    headings = [heading for group in SECTIONS.values() for heading in group]
    positions = [result.stdout.index(f"\n{heading}\n") for heading in headings]
    assert positions == sorted(positions)
    for heading in headings:
        assert f"\n\n{heading}\n{RULE}\n\n" in result.stdout
    rows = list(COMMAND.finditer(result.stdout))
    assert len(rows) == len(public_targets())
    assert {row[1] for row in rows} == set(public_targets())
    assert {row.start(2) - row.start() for row in rows} == {32}
    for row, next_row in pairwise(rows):
        between = result.stdout[row.end() : next_row.start()]
        if RULE not in between:
            assert between == "\n"
    assert "make check\n    Configure Azure:" in result.stdout
    assert max(map(len, result.stdout.splitlines())) <= 100
    assert result.stdout.endswith("\n\n")
    assert "\x1b" not in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("group", SECTIONS)
def test_help_can_focus_on_one_workflow(tmp_path, group):
    result = run_make(tmp_path, "help", f"GROUP={group}")
    assert result.returncode == 0, result.stderr
    expected = {target for target, owner in public_targets().items() if owner == group}
    assert {row[1] for row in COMMAND.finditer(result.stdout)} == expected
    for owner, headings in SECTIONS.items():
        for heading in headings:
            assert (f"\n{heading}\n" in result.stdout) == (owner == group)
    if group in ("azure", "local"):
        other = "local" if group == "azure" else "azure"
        assert f"make init ENV={other}" not in result.stdout
        assert f"RUN_{other.upper()}_SCENARIOS.md" not in result.stdout
    elif group == "checks":
        assert "make init" not in result.stdout
        assert "README.md#checks" in result.stdout


def test_help_contains_examples_guards_and_real_walkthroughs(tmp_path):
    result = run_make(tmp_path, "help", "GROUP=all")
    assert result.returncode == 0, result.stderr
    for text in (
        "make check",
        "make init ENV=azure",
        "make init ENV=local",
        "make endpoints ARGS=all",
        "make api ARGS='management GET /tenants/alpha'",
        "CONFIRM_LOCAL=yes",
        "CONFIRM_AZURE=yes",
        "disposable dependencies",
        "no saved export is required",
        "no record path is required",
        "Revalidate selected artifacts",
        "prepared Azure uses bootstrap",
    ):
        assert text in result.stdout
    for guide in ("RUN_AZURE_SCENARIOS.md", "RUN_LOCAL_SCENARIOS.md"):
        assert guide in result.stdout
        assert (ROOT / guide).is_file()


@pytest.mark.parametrize("group", ["all", *SECTIONS])
@pytest.mark.parametrize("color", ["never", "always"])
def test_guidance_is_distinct_from_the_command_catalog(tmp_path, group, color):
    result = run_make(tmp_path, "help", f"GROUP={group}", f"COLOR={color}")
    assert result.returncode == 0, result.stderr
    plain = ANSI.sub("", result.stdout)
    marker = "\n\n  Start here\n\n"
    assert marker in plain
    footer = plain.split(marker, 1)[1]
    next_heading = "Read next" if group == "checks" else "Walkthroughs"
    assert f"\n\n  {next_heading}\n\n" in footer
    assert RULE not in footer
    styled_footer = result.stdout.split("Start here", 1)[1]
    values = re.findall(r"^    [^:\n]+:\s+([^\n]+)$", styled_footer, re.MULTILINE)
    assert values
    assert all("\x1b" not in value for value in values)
    if color == "always":
        assert "\x1b[1mStart here\x1b[0m" in result.stdout
        assert "\x1b[34m" not in styled_footer


def test_invalid_help_group_fails_with_an_actionable_error(tmp_path):
    result = run_make(tmp_path, "help", "GROUP=unknown")
    assert result.returncode != 0
    assert "Use GROUP=all, setup, checks, local, or azure." in result.stderr
    assert result.stdout == ""
    assert list(tmp_path.iterdir()) == []


def test_each_public_command_has_a_stage_heading():
    source = (ROOT / "Makefile").read_text()
    for target in public_targets():
        if target == "help":
            continue
        assert re.search(rf"^{target}:.*\n\t\$\(SECTION\)\n", source, re.MULTILINE)


@pytest.mark.parametrize(
    "target,arguments",
    [
        ("lint", ["ruff", "check", "src", "scripts", "tests", "infra/bootstrap/tests"]),
        ("build", ["build"]),
        ("local-build", ["build"]),
    ],
)
@pytest.mark.parametrize("exit_code", [0, 7])
def test_stage_headings_preserve_tool_output_arguments_and_failures(
    tmp_path, target, arguments, exit_code
):
    tool = tmp_path / "tool.py"
    tool.write_text(
        "import sys\n"
        f"assert sys.argv[1:] == {arguments!r}\n"
        "print('tool output line 1\\ntool output line 2')\n"
        "print('tool diagnostic', file=sys.stderr)\n"
        f"raise SystemExit({exit_code})\n"
    )
    result = run_make(
        tmp_path,
        target,
        f"RUN={sys.executable} {tool}",
        f"STAGE={sys.executable} {tool}",
        "ENV=azure",
        "CONFIRM_AZURE=yes",
        "CONFIRM_LOCAL=yes",
    )
    assert result.stdout == "tool output line 1\ntool output line 2\n"
    assert result.stderr.startswith(f"\n{target}\n")
    assert f"\n  {target}\n" not in result.stderr
    assert "tool diagnostic\n" in result.stderr
    assert f"{'OK  ' if exit_code == 0 else 'ERROR: '}{target}" in result.stderr
    assert (result.returncode == 0) == (exit_code == 0)
    assert "All source checks passed" not in result.stdout


@pytest.mark.parametrize(
    "failed_stage",
    [None, "lint", "check-bicep", "test", "check-shell", "check-terraform"],
)
def test_check_groups_real_stages_and_reports_success_only_when_all_pass(tmp_path, failed_stage):
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = (
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "name = Path(sys.argv[0]).name\n"
        "if name == 'run':\n"
        "    if sys.argv[1] == 'ruff':\n"
        "        stage = 'lint'\n"
        "    elif sys.argv[1] == 'pytest':\n"
        "        stage = 'test'\n"
        "    else:\n"
        "        assert sys.argv[1:] == ['python', 'scripts/operations/local/validate.py']\n"
        "        stage = 'check-terraform'\n"
        "else:\n"
        "    stage = {'rad': 'check-bicep', 'bicep': 'check-bicep',\n"
        "             'shellcheck': 'check-shell'}[name]\n"
        "print(f'tool output: {stage}')\n"
        "raise SystemExit(7 if os.environ.get('FAILED_STAGE') == stage else 0)\n"
    )
    for name in ("run", "rad", "bicep", "shellcheck"):
        executable = binary / name
        executable.write_text(stub)
        executable.chmod(0o700)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "example.sh").write_text("#!/bin/sh\n")
    result = run_make(
        tmp_path,
        "check",
        f"RUN={binary / 'run'}",
        f"BICEP={binary / 'bicep'}",
        environment={
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "FAILED_STAGE": failed_stage or "",
        },
    )
    stages = ["lint", "check-bicep", "test", "check-shell", "check-terraform", "check"]
    expected = stages if failed_stage is None else stages[: stages.index(failed_stage) + 1]
    assert (
        re.findall(
            r"^(lint|check-bicep|test|check-shell|check-terraform|check)$",
            result.stderr,
            re.MULTILINE,
        )
        == expected
    )
    assert (result.returncode == 0) == (failed_stage is None)
    assert ("All source checks passed." in result.stdout) == (failed_stage is None)
    assert ("No deployment was performed." in result.stdout) == (failed_stage is None)


@pytest.mark.parametrize(
    "color,terminal,environment,styled",
    [
        ("auto", None, {}, False),
        ("auto", "stdout", {}, True),
        ("auto", "stderr", {}, False),
        ("auto", "stdout", {"TERM": "dumb"}, False),
        ("auto", "stdout", {"TERM": ""}, False),
        ("auto", "stdout", {"NO_COLOR": "1"}, False),
        ("always", None, {}, True),
        ("always", "stdout", {"TERM": "dumb"}, True),
        ("always", "stdout", {"NO_COLOR": "1"}, False),
        ("never", "stdout", {}, False),
    ],
)
def test_help_styles_only_the_selected_output_stream(
    tmp_path, color, terminal, environment, styled
):
    result = run_make(
        tmp_path,
        "help",
        "GROUP=checks",
        f"COLOR={color}",
        environment=environment,
        terminal=terminal,
    )
    plain = run_make(tmp_path, "help", "GROUP=checks", "COLOR=never")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert ANSI.sub("", result.stdout) == plain.stdout
    assert ("\x1b" in result.stdout) == styled
    if styled:
        assert "\x1b[1mChecks and tests\x1b[0m" in result.stdout
        assert f"\n{RULE}\n" in result.stdout
        assert f"\x1b[1m{'lint':<28}\x1b[0m" in result.stdout


@pytest.mark.parametrize(
    "color,terminal,environment,styled",
    [
        ("auto", None, {}, False),
        ("auto", "stdout", {}, False),
        ("auto", "stderr", {}, True),
        ("auto", "stderr", {"TERM": "dumb"}, False),
        ("auto", "stderr", {"NO_COLOR": "1"}, False),
        ("always", None, {}, True),
        ("always", "stderr", {"NO_COLOR": "1"}, False),
        ("never", "stderr", {}, False),
    ],
)
def test_styled_stage_headings_preserve_json_stdout(tmp_path, color, terminal, environment, styled):
    tool = tmp_path / "tool.py"
    tool.write_text('print(\'{"result":"unchanged"}\')\n')
    result = run_make(
        tmp_path,
        "show-config",
        f"RUN={sys.executable} {tool}",
        f"COLOR={color}",
        environment=environment,
        terminal=terminal,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"result":"unchanged"}\n'
    plain = ANSI.sub("", result.stderr)
    assert plain.startswith("\nshow-config\n")
    marker = f"{chr(0x2713)} " if styled else "OK  "
    assert f"  {marker}show-config completed\n" in plain
    assert "\n  show-config\n" not in plain
    assert "completed (" not in plain
    assert ("\x1b" in result.stderr) == styled


def test_invalid_color_fails_before_help_or_command_execution(tmp_path):
    for target in ("help", "show-config"):
        result = run_make(tmp_path, target, "COLOR=invalid", "RUN=false")
        assert result.returncode != 0
        assert "Use COLOR=auto, always, or never." in result.stderr
        assert result.stdout == ""
    assert list(tmp_path.iterdir()) == []


def test_build_forwards_explicit_recovery_arguments(tmp_path):
    tool = tmp_path / "stage.py"
    tool.write_text(
        "import sys\n"
        "assert sys.argv[1:] == ['build', '--recover-build', 'api=ch1']\n"
        "print('recovery arguments forwarded')\n"
    )
    result = run_make(
        tmp_path,
        "build",
        "CONFIRM_AZURE=yes",
        "ARGS=--recover-build api=ch1",
        f"STAGE={sys.executable} {tool}",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "recovery arguments forwarded\n"


@pytest.mark.parametrize(
    "color,terminal,columns",
    [("never", None, None), ("always", None, None), ("auto", "stderr", 32)],
)
def test_real_bootstrap_wrapper_chain_has_one_header_and_keeps_failure_diagnostics(
    tmp_path, color, terminal, columns
):
    for relative in (
        "scripts/operations/stage.sh",
        "scripts/operations/azure/bootstrap.sh",
        "scripts/lib/env.sh",
        "scripts/lib/output.sh",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    (tmp_path / ".env").write_text(
        'DEMO_ENV="azure"\nDEMO_PROJECT="sample"\nDEMO_DEPLOYMENT="demo"\n'
        'AZURE_SUBSCRIPTION_ID="11111111-1111-1111-1111-111111111111"\n'
        'AZURE_LOCATION="eastus2"\n'
    )
    (tmp_path / ".env").chmod(0o600)
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts.operations.azure import bootstrap as subject\n"
        "from scripts.operations.output import run_main, status\n"
        f"subject.ROOT = Path({str(tmp_path)!r})\n"
        "assert sys.argv[1] == str(subject.ROOT / 'scripts/operations/azure/bootstrap.py')\n"
        "sys.argv = sys.argv[1:]\n"
        "subject.check_source = lambda _: 'a' * 40\n"
        "subject.base_deployment = lambda _: None\n"
        "def execute(argv, **kwargs):\n"
        "    foundation = subject.ROOT / 'scripts/operations/azure/foundation.sh'\n"
        "    assert argv == ['bash', str(foundation)]\n"
        "    status('section', 'Bootstrap: configuration and tools')\n"
        "    print('native diagnostic remains visible', file=sys.stderr)\n"
        "    raise subject.EnvironmentError('synthetic failure before deployment')\n"
        "subject.execute = execute\n"
        "try:\n"
        "    sys.exit(run_main(subject.main, 'Azure bootstrap', announce=False))\n"
        "except subject.EnvironmentError as error:\n"
        "    status('error', str(error))\n"
        "    sys.exit(7)\n"
    )
    python.chmod(0o700)
    result = run_make(
        tmp_path,
        "bootstrap",
        "CONFIRM_AZURE=yes",
        f"COLOR={color}",
        terminal=terminal,
        columns=columns,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    plain = ANSI.sub("", result.stderr)
    rule = "-" * min(44, columns or 80)
    assert plain.startswith(
        "\nAzure bootstrap\ndemo | eastus2 | shared\n\n"
        "1 / 4  Prepare foundation\n" + rule + "\n"
        "  Next: Build images and Recipes\n\n  Configuration and tools\n"
    )
    assert plain.count("\nAzure bootstrap\n") == 1
    assert "== bootstrap ==" not in plain and "\n  bootstrap\n" not in plain
    assert "Azure environment bootstrap" not in plain
    assert "native diagnostic remains visible" in plain
    assert "ERROR: synthetic failure before deployment" in plain
    assert "completed" not in plain and "\n2 / 4" not in plain
    assert "\n\n\n" not in plain
