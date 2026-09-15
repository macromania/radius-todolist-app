import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "acr_build_provenance", ROOT / "scripts/operations/azure/build_provenance.py"
)
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)
REVISION = "c" * 40
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def record(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_text("committed canonical input\n")
    fingerprint = proof.source_fingerprint(source, "api", REVISION)
    staging = "plane-api:build-" + REVISION + "-" + "1" * 32
    run = {
        "runId": "ca1",
        "status": "Succeeded",
        "runType": "QuickBuild",
        "platform": {"os": "linux", "architecture": "amd64"},
        "outputImages": [
            {
                "registry": "acrdemo.azurecr.io",
                "repository": "plane-api",
                "tag": staging.split(":")[1],
                "digest": DIGEST,
            }
        ],
    }
    fresh = proof.fresh_record(
        {"runId": "ca1"}, run, "acrdemo.azurecr.io", "api", REVISION, fingerprint, staging
    )
    registry = {"tags": {fresh["key"]: fresh["value"] + ":" + "b" * 64}}
    return source, fingerprint, run, registry


def test_arm_record_matches_authenticated_run_and_canonical_context(record):
    _, fingerprint, run, registry = record
    verified = proof.verify_record(
        registry, run, "acrdemo.azurecr.io", "api", REVISION, fingerprint, DIGEST
    )
    assert verified["digest"] == DIGEST
    assert verified["run_id"] == "ca1"
    assert verified["filesystem_sha256"] == "b" * 64


def test_run_platform_accepts_service_enum_casing(record):
    _, fingerprint, run, registry = record
    run["platform"]["os"] = "Linux"
    assert (
        proof.verify_record(
            registry, run, "acrdemo.azurecr.io", "api", REVISION, fingerprint, DIGEST
        )["digest"]
        == DIGEST
    )


@pytest.mark.parametrize("change", ["digest", "run", "context", "missing", "old-policy"])
def test_publisher_metadata_cannot_supply_or_replace_arm_proof(record, change):
    _, fingerprint, run, registry = record
    digest = DIGEST
    key = proof.proof_key("api", REVISION)
    if change == "digest":
        digest = "sha256:" + "f" * 64
    elif change == "run":
        run["outputImages"][0]["digest"] = "sha256:" + "f" * 64
    elif change == "context":
        fingerprint = "d" * 64
    elif change == "missing":
        registry["tags"].clear()
    else:
        registry["tags"][key] = registry["tags"][key].replace("v2:", "v1:")
    with pytest.raises(proof.ProvenanceError):
        proof.verify_record(
            registry, run, "acrdemo.azurecr.io", "api", REVISION, fingerprint, digest
        )


def test_context_binds_source_modes_and_worker_api_base(record):
    source, initial, _, _ = record
    path = source / "Dockerfile"
    path.chmod(0o700)
    assert proof.source_fingerprint(source, "api", REVISION) != initial
    path.write_text("changed inputs\n")
    assert proof.source_fingerprint(source, "api", REVISION) != initial
    one = proof.source_fingerprint(
        source, "provisioner", REVISION, "acrdemo.azurecr.io/plane-api@" + DIGEST
    )
    two = proof.source_fingerprint(
        source, "provisioner", REVISION, "acrdemo.azurecr.io/plane-api@sha256:" + "f" * 64
    )
    assert one != two


@pytest.mark.parametrize("status", ["Failed", "Running", "Queued", "Canceled"])
def test_only_successful_quick_build_run_outputs_can_be_attested(record, status):
    _, fingerprint, run, _ = record
    run["status"] = status
    with pytest.raises(proof.ProvenanceError, match="untrusted_acr_build_run"):
        proof.fresh_record(
            {"runId": "ca1"},
            run,
            "acrdemo.azurecr.io",
            "api",
            REVISION,
            fingerprint,
            "plane-api:build-" + REVISION + "-" + "1" * 32,
        )
