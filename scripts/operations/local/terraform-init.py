"""Populate Radius's writable Terraform volume from the prepared operator image."""

import os
import shutil
import stat
from pathlib import Path


def initialize(source: Path = Path("/opt/radplanes"), target: Path = Path("/terraform")) -> None:
    binary = source / "terraform"
    providers = source / "providers"
    for path in (
        source,
        binary,
        providers,
        source / "terraform.tfrc",
        *providers.rglob("*"),
        target,
        *target.rglob("*"),
    ):
        if path.is_symlink():
            raise RuntimeError("terraform_layout_symlink")
    configuration = (source / "terraform.tfrc").read_text()
    if (
        not binary.is_file()
        or not providers.is_dir()
        or not any(providers.rglob("*.zip"))
        or 'path    = "/opt/radplanes/providers"' not in configuration
        or "direct {" in configuration
        or target.is_symlink()
    ):
        raise RuntimeError("prepared_terraform_assets_missing")
    for directory in (target, target / ".terraform-global"):
        if directory.is_symlink():
            raise RuntimeError("terraform_layout_symlink")
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chown(directory, 0, 0)
        directory.chmod(0o700)
    for destination in (target / "terraform", target / ".terraform-global/terraform"):
        if destination.is_symlink():
            raise RuntimeError("terraform_layout_symlink")
        if destination.exists():
            os.chown(destination, 0, 0)
        shutil.copyfile(binary, destination)
        destination.chmod(0o700)
        os.chown(destination, 65532, 65532)
    mirror = target / "providers"
    if mirror.is_symlink():
        raise RuntimeError("terraform_layout_symlink")
    shutil.copytree(providers, mirror, dirs_exist_ok=True)
    config = target / "terraform.tfrc"
    if config.is_symlink():
        raise RuntimeError("terraform_layout_symlink")
    config.write_text(configuration.replace("/opt/radplanes/providers", str(mirror)))
    config.chmod(0o644)
    for item in (mirror, *mirror.rglob("*")):
        if item.is_symlink():
            raise RuntimeError("terraform_mirror_symlink")
        item.chmod(0o755 if item.is_dir() else 0o644)
    marker = target / ".terraform-global/.terraform-ready"
    if marker.is_symlink():
        raise RuntimeError("terraform_layout_symlink")
    if marker.exists():
        metadata = marker.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("terraform_marker_not_private")
        os.chown(marker, 0, 0)
    marker.touch(mode=0o600)
    marker.chmod(0o600)
    os.chown(marker, 65532, 65532)
    for directory in (target / ".terraform-global", target):
        os.chown(directory, 65532, 65532)


if __name__ == "__main__":
    initialize()
