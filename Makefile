SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV ?= azure
CONFIG ?= .state/azure/provisioning.json
ACCEPTANCE_CONFIG ?= .state/azure/acceptance.json
SLOT ?= management
BICEP ?= $(HOME)/.rad/bin/bicep
RUN := uv run --no-sync
export CONFIRM_AZURE CONFIRM_LOCAL
export PYTHONDONTWRITEBYTECODE := 1
export TMPDIR := $(CURDIR)/.state/check/tmp

.PHONY: help check lint test test-integration check-bicep check-shell check-terraform check-work \
        require-azure confirm-azure preflight bootstrap-preview validate-azure bootstrap \
        install-radius publish-recipes build-publish register-radius deploy-management \
        deploy-management-preview export-state test-e2e test-outages clean-plan clean-azure verify-clean \
        local-prepare local-executor-build local-executor-inspect local-bootstrap local-install-radius \
        confirm-local local-runtime-build local-runtime-inspect local-runtime-load \
        local-setup local-deploy-management local-export local-test local-clean-plan \
        local-clean local-verify

help: ## List source, Azure, and explicit local commands
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'

check-work:
	@mkdir -p "$(TMPDIR)" .state/check infra/radius/types/.build

check: lint test check-shell check-terraform ## Run cloud-free Python, Bicep, Terraform mock, and shell checks

lint: check-work ## Check runtime, operations, harness, and tests with Ruff
	$(RUN) ruff check src operations harness tests infra/bootstrap/tests

check-bicep: check-work ## Generate Radius extensions and compile every current Bicep file
	@set -euo pipefail; \
	for type in clusters postgresql gateways; do \
	  rad --config "$(CURDIR)/.state/check/radius.yaml" bicep publish-extension \
	    --from-file "infra/radius/types/$$type.yaml" \
	    --target "infra/radius/types/$$type.tgz" --force; \
	done; \
	for file in infra/bootstrap/*.bicep infra/radius/recipes/azure/*.bicep \
	  infra/radius/apps/*.bicep infra/radius/modules/*.bicep \
	  infra/radius/environments/*.bicep; do \
	  printf '==> %s\n' "$$file"; \
	  "$(BICEP)" build "$$file" --stdout >/dev/null; \
	done

test: check-bicep ## Run offline suites, including compiled infrastructure contracts
	env -u TEST_POSTGRES_DSN -u TEST_ALLOW_DATABASE_CREATE -u TEST_REDIS_URL \
	  -u TEST_KUBECONFIG -u TEST_KUBE_CONTEXT -u TEST_KUBE_NAMESPACE \
	  RADIUS_BICEP="$(BICEP)" $(RUN) pytest -q tests infra/bootstrap/tests

test-integration: check-work ## Run dependency tests with explicitly configured disposable databases
	$(RUN) pytest -q tests/integration

check-shell: ## Check shell entrypoints without executing them
	shellcheck operations/*.sh harness/*.sh infra/radius/recipes/local/cluster/*.sh

check-terraform: check-work ## Validate the local Recipe with mocked providers; never creates clusters
	$(RUN) python operations/local/validate.py

confirm-local: check-work
	@test "$(CONFIRM_LOCAL)" = yes || { echo "Set CONFIRM_LOCAL=yes for local Docker/Kubernetes mutations." >&2; exit 1; }

local-prepare: check-work ## Prepare local Recipe bundles and manifests without creating resources
	$(RUN) python operations/local/prepare.py

local-executor-build: confirm-local ## Build the local Radius executor/operator images
	$(RUN) python operations/local/images.py build --execute

local-executor-inspect: confirm-local ## Verify the local Radius executor/operator image contents
	$(RUN) python operations/local/images.py inspect --execute

local-bootstrap: confirm-local ## Create the management kind cluster and verify Secret encryption
	$(RUN) python operations/local/bootstrap.py create --execute

local-install-radius: confirm-local ## Install Radius and its executor in the management cluster
	$(RUN) python operations/local/bootstrap.py install --execute

local-runtime-build: confirm-local ## Build native local API/provisioner images from committed inputs
	$(RUN) python operations/local/runtime-images.py build --execute

local-runtime-inspect: confirm-local ## Verify immutable local image filesystems and import smoke checks
	$(RUN) python operations/local/runtime-images.py inspect --execute

local-runtime-load: confirm-local ## Load inspected runtime images into the existing management cluster only
	$(RUN) python operations/local/runtime-images.py load-management --execute

local-setup: confirm-local ## Register full-demo Recipes and config in the verified local management cluster
	$(RUN) python operations/local/setup-demo.py --execute

local-deploy-management: confirm-local ## Deploy local management through Radius; no child cluster creation
	$(RUN) python operations/local/deploy-demo.py --execute

local-export: check-work ## Export verified local topology/API access once; 3 means not ready yet
	$(RUN) python harness/local/export-state.py --once

local-test: confirm-local ## Run all local admissions, isolation checks, and both real parent outages
	$(RUN) python harness/test-e2e.py --config .state/local/acceptance.json --mode all --execute

local-clean-plan: check-work ## Preview exact local ownership and ordered whole-demo cleanup
	$(RUN) python operations/local/cleanup.py

local-clean: confirm-local ## Remove the verified local demo in Radius ownership order
	$(RUN) python operations/local/cleanup.py --execute

local-verify: check-work ## Verify a retained local cleanup record without contacting deleted clusters
	@test -n "$(LOCAL_CLEANUP_RECORD)" || { echo "Set LOCAL_CLEANUP_RECORD to the exact cleanup record." >&2; exit 1; }
	$(RUN) python operations/local/cleanup.py --verify "$(LOCAL_CLEANUP_RECORD)"

require-azure: check-work
	@test "$(ENV)" = azure || { echo "This target is Azure-only; use the explicit local-* targets." >&2; exit 1; }

confirm-azure: require-azure
	@test "$(CONFIRM_AZURE)" = yes || { echo "Set CONFIRM_AZURE=yes for Azure mutations." >&2; exit 1; }

preflight: require-azure ## Inspect Azure prerequisites and write scoped local context
	$(RUN) python operations/project.py preflight --environment "$(ENV)"

bootstrap-preview: require-azure ## Prepare bootstrap inputs and run Azure what-if
	$(RUN) python operations/project.py bootstrap-preview --environment "$(ENV)"

validate-azure: require-azure ## Optionally run Azure what-if/validation and record diagnostic hashes
	$(RUN) python operations/validate-bootstrap.py

bootstrap: confirm-azure ## Compile and deploy the scoped Azure foundation
	$(RUN) python operations/project.py bootstrap --environment "$(ENV)"

install-radius: confirm-azure ## Install management Radius after bootstrap
	$(RUN) python operations/project.py install-radius --environment "$(ENV)"

publish-recipes: confirm-azure ## Publish and verify immutable Recipe tags in the project ACR
	$(RUN) python operations/publish-artifacts.py

build-publish: confirm-azure ## Build/push committed images in ACR; content verification remains required
	$(RUN) python operations/build-images.py

register-radius: confirm-azure ## Register types/environment in an existing configured cluster
	$(RUN) python operations/register-radius.py --slot "$(SLOT)" --config "$(CONFIG)"

deploy-management: confirm-azure ## Submit the management operator Job; verify completion separately
	$(RUN) python operations/run-management-job.py --config "$(CONFIG)" --execute

deploy-management-preview: require-azure ## Prepare the management Job manifest without submitting it
	$(RUN) python operations/run-management-job.py --config "$(CONFIG)"

export-state: require-azure ## Export protected harness state once; exit 3 means not ready yet
	$(RUN) python harness/export-state.py --config "$(CONFIG)" --once

test-e2e: confirm-azure ## Run the opt-in live Azure onboarding scenario
	$(RUN) python harness/test-e2e.py --config "$(ACCEPTANCE_CONFIG)" --mode scenario --execute

test-outages: confirm-azure ## Run opt-in live parent-link outages and restoration
	$(RUN) python harness/test-e2e.py --config "$(ACCEPTANCE_CONFIG)" --mode outages --execute

clean-plan: require-azure ## Read cloud ownership and print the ordered cleanup plan
	$(RUN) python operations/clean-azure.py

clean-azure: confirm-azure ## Delete only verified project-owned Azure resources in owner order
	$(RUN) python operations/clean-azure.py --execute

verify-clean: require-azure ## Verify actual Azure deletion and report retained recovery records
	$(RUN) python operations/verify-clean.py
