import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def test_license_and_package_metadata_agree():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert (
        (ROOT / "LICENSE")
        .read_text()
        .startswith("MIT License\n\nCopyright (c) 2026 radius-todolist-app contributors\n")
    )
    assert "COPY pyproject.toml uv.lock LICENSE ./" in (ROOT / "images/api/Dockerfile").read_text()


def test_locked_dependencies_use_public_sources_without_credentials():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["tool"]["uv"]["index"] == [
        {"name": "pypi", "url": "https://pypi.org/simple", "default": True}
    ]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    for package in lock["package"]:
        source = package["source"]
        if "registry" not in source:
            assert source == {"editable": "."}
            continue
        assert source["registry"] == "https://pypi.org/simple"
        artifacts = [*package.get("wheels", [])]
        if "sdist" in package:
            artifacts.append(package["sdist"])
        assert artifacts, package["name"]
        for artifact in artifacts:
            parsed = urlsplit(artifact["url"])
            assert parsed.scheme == "https" and parsed.hostname == "files.pythonhosted.org"
            assert not parsed.username and not parsed.password and not parsed.query
            assert artifact["hash"].startswith("sha256:")


def test_terraform_executor_preserves_distributed_license():
    dockerfile = (ROOT / "images/radius-kind/Dockerfile").read_text()
    assert "unzip /tmp/terraform.zip -d /tools" in dockerfile
    assert "COPY --from=tools /tools/LICENSE.txt /opt/radplanes/LICENSE.terraform" in dockerfile


def test_gitignore_has_no_duplicate_rules():
    rules = [
        line
        for line in (ROOT / ".gitignore").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert len(rules) == len(set(rules))
    assert ".local" in (ROOT / ".dockerignore").read_text().splitlines()
