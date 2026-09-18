import copy
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
        "runType": "QuickRun",
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


@pytest.mark.parametrize("run_type", ["QuickBuild", "QuickRun"])
def test_supported_docker_build_run_types(record, run_type):
    _, fingerprint, run, registry = record
    run["runType"] = run_type
    assert (
        proof.verify_record(
            registry, run, "acrdemo.azurecr.io", "api", REVISION, fingerprint, DIGEST
        )["run_id"]
        == "ca1"
    )


@pytest.mark.parametrize("run_type", ["AutoBuild", "AutoRun", "TaskRun", None])
def test_other_run_types_are_not_build_provenance(record, run_type):
    _, fingerprint, run, registry = record
    run["runType"] = run_type
    with pytest.raises(proof.ProvenanceError, match="untrusted_acr_build_run"):
        proof.verify_record(
            registry, run, "acrdemo.azurecr.io", "api", REVISION, fingerprint, DIGEST
        )


def test_run_resolution_uses_exact_tag_not_latest_run_or_digest(record):
    _, _, run, _ = record
    other = copy.deepcopy(run)
    other["runId"] = "newer"
    other["outputImages"][0]["tag"] = "build-" + REVISION + "-" + "2" * 32
    staging = "plane-api:" + run["outputImages"][0]["tag"]
    assert proof.resolve_run([other, run], "acrdemo.azurecr.io", "api", REVISION, staging) == run
    for runs in ([], [other], [run, copy.deepcopy(run)]):
        with pytest.raises(proof.ProvenanceError, match="missing_or_ambiguous"):
            proof.resolve_run(runs, "acrdemo.azurecr.io", "api", REVISION, staging)


def test_pending_receipt_is_source_bound_and_not_a_completed_proof(record):
    _, fingerprint, _, registry = record
    pending = proof.intent_record("api", REVISION, fingerprint, "1" * 32)
    registry["tags"][pending["key"]] = pending["value"]
    assert proof.build_state(registry, "api", REVISION, fingerprint) == pending
    with pytest.raises(proof.ProvenanceError, match="pending_build_context"):
        proof.build_state(registry, "api", REVISION, "f" * 64)
    with pytest.raises(proof.ProvenanceError, match="arm_build_provenance"):
        proof.record_parts(registry, "api", REVISION, fingerprint, DIGEST)


def test_recovery_requires_matching_build_instructions_and_source_time(record):
    source, _, run, _ = record
    dockerfile = source / "images/api/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        "FROM synthetic@sha256:" + "a" * 64 + "\nRUN echo \\\n  'synthetic value'\n"
    )
    run["createTime"] = "2026-09-17T11:00:00+00:00"
    fingerprint = proof.source_fingerprint(source, "api", REVISION)
    logs = f"Step 1/2 : FROM synthetic@sha256:{'a' * 64}\nStep 2/2 : RUN echo 'synthetic value'\n"
    recovered = proof.recovery_record(
        source, run, logs, "acrdemo.azurecr.io", "api", REVISION, fingerprint, "", 1700000000
    )
    assert recovered["origin"] == "recovery-v1" and recovered["runId"] == "ca1"
    for invalid in (
        logs.replace("synthetic value", "different"),
        logs.replace("'synthetic value'", '"synthetic value"'),
        logs + "Step 3/3 : RUN evil\n",
    ):
        with pytest.raises(proof.ProvenanceError, match="recovery_build_instructions"):
            proof.recovery_record(
                source,
                run,
                invalid,
                "acrdemo.azurecr.io",
                "api",
                REVISION,
                fingerprint,
                "",
                1700000000,
            )
    with pytest.raises(proof.ProvenanceError, match="predates_source"):
        proof.recovery_record(
            source, run, logs, "acrdemo.azurecr.io", "api", REVISION, fingerprint, "", 2000000000
        )


def test_legacy_recovery_does_not_infer_a_provisioner_base(record):
    source, fingerprint, run, _ = record
    with pytest.raises(proof.ProvenanceError, match="explicit_recovery_requires_api_build"):
        proof.recovery_record(
            source,
            run,
            "",
            "acrdemo.azurecr.io",
            "provisioner",
            REVISION,
            fingerprint,
            "acrdemo.azurecr.io/plane-api@" + DIGEST,
            1700000000,
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
