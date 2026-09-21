import ast
import collections
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DOCS = (
    "README.md",
    "RUN_AZURE_SCENARIOS.md",
    "RUN_LOCAL_SCENARIOS.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    ".github/pull_request_template.md",
)


def test_documentation_is_limited_to_guides_and_contributor_policies():
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert set(filter(None, result.stdout.split("\0"))) <= set(PUBLIC_DOCS)
    assert all((ROOT / name).is_file() for name in PUBLIC_DOCS)


def prose(text):
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def anchors(text):
    seen = collections.Counter()
    result = set()
    for heading in re.findall(r"^#{1,6}\s+(.+)$", prose(text), re.MULTILINE):
        slug = re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
        result.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)
        seen[slug] += 1
    return result


@pytest.mark.parametrize("name", PUBLIC_DOCS)
def test_surviving_document_links_are_self_contained_and_resolve(name):
    path = ROOT / name
    text = path.read_text()
    for link in re.findall(r"\[[^\]]+\]\(([^)\s]+)\)", prose(text)):
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc:
            continue
        target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        assert target.is_relative_to(ROOT), link
        assert target.exists(), f"{name}: {link}"
        if target.suffix == ".md":
            assert target.relative_to(ROOT).as_posix() in PUBLIC_DOCS, f"{name}: {link}"
            if parsed.fragment:
                assert unquote(parsed.fragment) in anchors(target.read_text()), f"{name}: {link}"


@pytest.mark.parametrize("environment", ["AZURE", "LOCAL"])
def test_manual_guide_shell_blocks_parse_and_use_current_targets(environment):
    text = (ROOT / f"RUN_{environment}_SCENARIOS.md").read_text()
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", (ROOT / "Makefile").read_text(), re.MULTILINE))
    blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", text, re.DOTALL)
    assert blocks
    for block in blocks:
        result = subprocess.run(
            ["bash", "-n"], input=block, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        for target in re.findall(r"^\s*make ([a-z][a-z0-9-]*)", block, re.MULTILINE):
            assert target in targets, target
        assert "$STATE" not in block
        assert "acceptance.json" not in block
        assert "LOCAL_CLEANUP_RECORD" not in block
        assert "--config " not in block
        assert not re.search(r"\b(?:api|k|report|journal|endpoint)\(\)\s*\{", block)
        assert not re.search(r"^\s*(?:api|k|report|journal|endpoint)\s", block, re.MULTILINE)
    assert "make fault-status" in text and "make report" in text
    assert "git worktree add" not in text
    assert ".state/azure/provisioning.json" not in text
    assert "Assemble protected" not in text
    if environment == "LOCAL":
        assert "shared-clusters-before.txt" in text and "shared-clusters-after.txt" in text
    else:
        assert "unchanged cluster UIDs" in text
        assert "matching `pair_id` values" in text
    assert "make verify-clean" in text


@pytest.mark.parametrize(
    ("environment", "source", "class_name"),
    [
        ("AZURE", "scripts/operations/clean-azure.py", "LiveAzureCleanup"),
        ("LOCAL", "scripts/operations/local/cleanup.py", "LiveLocalCleanup"),
    ],
)
def test_cleanup_checkpoint_matches_the_live_entrypoint(environment, source, class_name):
    tree = ast.parse((ROOT / source).read_text())
    engine = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    verify = next(
        node for node in engine.body if isinstance(node, ast.FunctionDef) and node.name == "verify"
    )
    returned = next(node.value for node in verify.body if isinstance(node, ast.Return))
    constants = {
        key.value: value.value
        for key, value in zip(returned.keys, returned.values, strict=True)
        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
    }
    guide = (ROOT / f"RUN_{environment}_SCENARIOS.md").read_text()
    for key in ("status", "scope"):
        assert f"{key}: {constants[key]}" in guide
    assert "`resources_removed`" not in guide


@pytest.mark.parametrize("environment", ["AZURE", "LOCAL"])
def test_busy_check_precedes_waiting_for_provisioning(environment):
    text = (ROOT / f"RUN_{environment}_SCENARIOS.md").read_text()
    section = text.split("### A.", 1)[1].split("### B.", 1)[0]
    check = "capacity-check" if environment == "AZURE" else "busy-check"
    assert section.index(f'"tenant_id":"{check}"') < section.index("#### Follow provisioning")
    assert "--prompt-demo-key" not in text
    assert "--demo-key-from-env SLOT=VARIABLE" in text


def test_azure_admission_uses_two_direct_requests_without_saved_responses():
    text = (ROOT / "RUN_AZURE_SCENARIOS.md").read_text()
    assert not re.search(r"\bnotes\b", text, re.IGNORECASE)
    assert "plane-manual." not in text and "diff -u" not in text
    section = text.split("### A.", 1)[1].split("#### Optional admission checks", 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
    requests = next(block for block in blocks if '"initial_message":"alpha"' in block)
    assert len(re.findall(r"^curl ", requests, re.MULTILINE)) == 2
    assert "$MANAGEMENT_URL/tenants" in requests
    assert "$MANAGEMENT_URL/operations/<operation-id>" in requests
    assert requests.count("X-Demo-Key: $MANAGEMENT_KEY") == 2
    assert "--fail-with-body" in requests
    assert "make api" not in requests and "jq " not in requests and "> " not in requests
    assert "management get secret management-api-runtime" in text


def test_local_prerequisites_cover_native_stage_tools():
    guide = (ROOT / "RUN_LOCAL_SCENARIOS.md").read_text()
    prerequisites = (
        guide.split("## 1. Prepare the workspace", 1)[1]
        .split("### Select operator configuration", 1)[0]
        .lower()
    )
    aliases = {"rad": "radius", "docker": "docker desktop"}
    for script in ("build.sh", "bootstrap.sh"):
        source = (ROOT / "scripts/operations/local" / script).read_text()
        declaration = re.search(r"for tool in ([^\n;]+); do", source)
        assert declaration is not None
        for tool in declaration[1].split():
            name = aliases.get(tool, tool)
            assert re.search(rf"\b{re.escape(name)}\b", prerequisites), f"{script}: {tool}"


def test_azure_shared_only_route_does_not_require_isolated_resources():
    guide = (ROOT / "RUN_AZURE_SCENARIOS.md").read_text()
    updates = guide.split("### D.", 1)[1].split("### E.", 1)[0]
    shared, isolated = updates.split("#### Optional: update the isolated tenant", 1)
    assert "isolated-1" not in shared and "isolated-c" not in shared
    assert "only if you completed section C" in isolated
    assert "control:isolated-1" in isolated and "data:isolated-1" in isolated
    faults = guide.split("### F.", 1)[1].split("## 4.", 1)[0]
    assert "three shared-demo endpoints" in faults
    assert "all five endpoints" not in faults
    assert not re.search(r"^\s*make .*isolated-1", faults, re.MULTILINE)
