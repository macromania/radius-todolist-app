# Contributing

Start with [README.md](README.md). The [Azure guide](RUN_AZURE_SCENARIOS.md) and
[local guide](RUN_LOCAL_SCENARIOS.md) are the technical documentation. Keep them
current when behavior changes; do not add historical implementation reports.

## Set up and check a change

Use Python 3.13 through uv and the tool versions listed in either guide. Then,
from the repository root:

```bash
uv sync --locked
make check
```

These checks need no Azure account, private `.env`, running Docker daemon, or
deployed database. They may download public tool and provider dependencies.
They run Ruff, Bicep compilation, Python tests, Terraform mock-provider checks,
and ShellCheck. Use the smallest relevant test selection while developing,
then run `make check` before submitting:

```bash
uv run --no-sync pytest -q tests/operations/test_manual_guides.py
```

Use the Radius Bicep compiler, not `az bicep`. Keep pinned dependencies, tool
checksums, and lockfiles together. The root `uv.lock` uses public PyPI.
If a machine-wide `UV_DEFAULT_INDEX` overrides it, append
`--default-index https://pypi.org/simple` to the sync command for this invocation.
Do not commit a lockfile pointing at an organization-specific mirror.
`scripts/operations/lock-tools.sh` refreshes the separate operator-image locks;
inspect version changes before committing them.

## Scope and safety

This is a trusted-operator demo, not a production SaaS platform. Preserve the
three-plane boundaries, child-initiated polling, Radius resource ownership,
separate runtime identities, TLS settings, and explicit cleanup checks.
Read [AGENTS.md](AGENTS.md) for the source map and contracts.

Live tests cost money or mutate local Docker/Kubernetes resources. Run them only
against your own disposable deployment after reading the selected guide.
`make test-integration` requires explicit disposable dependencies; do not point
it at a demo or shared database. Use Docker Desktop, the ports in `ports.env`,
and explicit CLI contexts. Never change global defaults for another project.

Do not commit `.env`, credentials, kubeconfigs, Terraform state, generated access
files, or raw deployment logs. Use synthetic test data and redact diagnostics.
Report vulnerabilities through [SECURITY.md](SECURITY.md), not a public issue.

## Submit a pull request

Explain the behavior change and its reason. Keep changes focused, add a regression
that exercises the real caller, and update affected guide commands/checkpoints.
For image changes, describe what code or permissions were inspected inside the
rebuilt image. A new digest alone is not verification.

Distinguish source checks from live observations. Include the tested revision
and environment for live results; do not imply that mocks prove a deployment.
Do not remove safety checks or skip failing regressions to make CI pass.

Contributions to this repository's original source are under the
[MIT license](LICENSE). Third-party dependencies retain their own terms; see
[Third-party software](THIRD_PARTY_NOTICES.md).
