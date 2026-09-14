SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV ?= azure
ARGS ?=
GROUP ?= all
CONFIG ?= .state/azure/provisioning.json
ACCEPTANCE_CONFIG ?= .state/azure/acceptance.json
SLOT ?= management
BICEP ?= $(HOME)/.rad/bin/bicep
RUN := uv run --no-sync
SECTION = @printf '\n== %s ==\n' '$@' >&2
export CONFIRM_AZURE CONFIRM_LOCAL
export PYTHONDONTWRITEBYTECODE := 1
CHECK_TMP := $(CURDIR)/.state/check/tmp
check lint test check-bicep check-terraform: export TMPDIR := $(CHECK_TMP)

.PHONY: help init show-config endpoints api check lint test test-integration check-bicep check-shell check-terraform check-work \
        require-azure confirm-azure preflight bootstrap-preview validate-azure bootstrap \
        install-radius publish-recipes build-publish register-radius deploy-management \
        deploy-management-preview export-state test-e2e test-outages clean-plan clean-azure verify-clean \
        local-prepare local-executor-build local-executor-inspect local-bootstrap local-install-radius \
        confirm-local local-runtime-build local-runtime-inspect local-runtime-load \
        local-setup local-deploy-management local-export local-test local-clean-plan \
        local-clean local-verify

##@ setup Setup and API
##! Choose ENV when initializing .env; pass only nonsecret options through ARGS.
help: ## Show grouped commands; use GROUP=setup, checks, local, or azure
	@awk -v group="$(GROUP)" '\
	  BEGIN { \
	    if (group !~ /^(all|setup|checks|local|azure)$$/) { \
	      print "Unknown help group. Use GROUP=all, setup, checks, local, or azure." > "/dev/stderr"; \
	      exit 2; \
	    } \
	    print "Radius three-plane demo"; \
	    print "Usage: make <target> [NAME=value]"; \
	    print "Focus: make help GROUP=azure  (groups: setup, checks, local, azure, all)"; \
	  } \
	  /^##@ / { \
	    show = (group == "all" || group == $$2); \
	    if (show) { sub(/^##@ [^ ]+ /, ""); printf "\n%s\n", $$0; } \
	    next; \
	  } \
	  /^##! / && show { printf "  %s\n\n", substr($$0, 5); next; } \
	  /^[a-zA-Z0-9_-]+:.*## / && show { \
	    split($$0, entry, ":.*## "); \
	    printf "  %-28s  %s\n", entry[1], entry[2]; \
	  } \
	  END { \
	    if (group ~ /^(all|setup|checks|local|azure)$$/) { \
	      print "\nStart here"; \
	      print "  Check source:     make check"; \
	      if (group != "checks") { \
	        if (group != "local") print "  Configure Azure:  make init ENV=azure"; \
	        if (group != "azure") print "  Configure local:  make init ENV=local"; \
	        print "  Find endpoints:   make endpoints ARGS=all"; \
	        print "  Call the API:     make api ARGS='\''management GET /tenants/alpha'\''"; \
	        print "\nWalkthroughs"; \
	        if (group != "local") print "  Azure: RUN_AZURE_SCENARIOS.md"; \
	        if (group != "azure") print "  Local: RUN_LOCAL_SCENARIOS.md"; \
	        print "  Follow the selected guide for prerequisites and manual handoffs."; \
	      } else { \
	        print "\nRead next"; \
	        print "  Dependency setup: docs/contracts.md#validation"; \
	      } \
	    } \
	  }' $(MAKEFILE_LIST)

init: ## Create private .env; choose ENV=azure or ENV=local
	$(SECTION)
	@bash scripts/operations/init.sh --environment "$(ENV)" $(ARGS)

show-config: ## Show selected .env configuration with secrets redacted
	$(SECTION)
	@$(RUN) python scripts/operations/demo.py config

endpoints: ## Discover live endpoints; ARGS=<slot> or ARGS=all
	$(SECTION)
	@bash scripts/operations/endpoints.sh $(ARGS)

api: ## Call ARGS='TARGET METHOD /path'; optional JSON on stdin
	$(SECTION)
	@bash scripts/operations/api.sh $(ARGS)

check-work:
	@mkdir -p "$(CHECK_TMP)" .state/check infra/radius/types/.build

##@ checks Checks and tests
##! make check is cloud-free. Integration tests need disposable dependencies.
check: lint test check-shell check-terraform ## Run all source checks without deploying anything
	$(SECTION)
	@printf 'All source checks passed. No deployment was performed.\n'

lint: check-work ## Run Ruff on Python source and tests
	$(SECTION)
	@$(RUN) ruff check src scripts tests infra/bootstrap/tests

check-bicep: check-work ## Generate Radius extensions and compile all Bicep
	$(SECTION)
	@set -euo pipefail; \
	for type in clusters postgresql gateways; do \
	  rad --config "$(CURDIR)/.state/check/radius.yaml" bicep publish-extension \
	    --from-file "infra/radius/types/$$type.yaml" \
	    --target "infra/radius/types/$$type.tgz" --force; \
	done; \
	for file in infra/bootstrap/*.bicep infra/radius/recipes/azure/*.bicep \
	  infra/radius/apps/*.bicep infra/radius/modules/*.bicep \
	  infra/radius/environments/*.bicep; do \
	  printf '  Compile %s\n' "$$file"; \
	  "$(BICEP)" build "$$file" --stdout >/dev/null; \
	done

test: check-bicep ## Run offline tests, including infrastructure contracts
	$(SECTION)
	@env -u TEST_POSTGRES_DSN -u TEST_ALLOW_DATABASE_CREATE -u TEST_REDIS_URL \
	  -u TEST_KUBECONFIG -u TEST_KUBE_CONTEXT -u TEST_KUBE_NAMESPACE \
	  RADIUS_BICEP="$(BICEP)" $(RUN) pytest -q tests infra/bootstrap/tests

test-integration: check-work ## Run opt-in tests against disposable dependencies
	$(SECTION)
	@$(RUN) pytest -q tests/integration

check-shell: ## Run ShellCheck on all shell entrypoints and libraries
	$(SECTION)
	@find scripts -type f -name '*.sh' -exec shellcheck --external-sources --source-path=SCRIPTDIR {} +

check-terraform: check-work ## Validate local Recipes with mocks; creates no clusters
	$(SECTION)
	@$(RUN) python scripts/operations/local/validate.py

confirm-local: check-work
	@test "$(CONFIRM_LOCAL)" = yes || { echo "Set CONFIRM_LOCAL=yes for local Docker/Kubernetes mutations." >&2; exit 1; }

##@ local Local: preparation and images
##! Use Docker Desktop. Image build/inspect commands need CONFIRM_LOCAL=yes.
local-prepare: check-work ## Prepare Recipe bundles and manifests; creates no resources
	$(SECTION)
	@$(RUN) python scripts/operations/local/prepare.py

local-executor-build: confirm-local ## Build Radius executor/operator images
	$(SECTION)
	@$(RUN) python scripts/operations/local/images.py build --execute

local-executor-inspect: confirm-local ## Verify executor/operator image contents
	$(SECTION)
	@$(RUN) python scripts/operations/local/images.py inspect --execute

local-runtime-build: confirm-local ## Build native API/provisioner images from committed inputs
	$(SECTION)
	@$(RUN) python scripts/operations/local/runtime-images.py build --execute

local-runtime-inspect: confirm-local ## Verify runtime image contents and imports
	$(SECTION)
	@$(RUN) python scripts/operations/local/runtime-images.py inspect --execute

##@ local Local: deployment and acceptance
##! Mutating commands require CONFIRM_LOCAL=yes. Run stages from the local guide.
local-bootstrap: confirm-local ## Create management kind cluster; verify Secret encryption
	$(SECTION)
	@$(RUN) python scripts/operations/local/bootstrap.py create --execute

local-install-radius: confirm-local ## Install management Radius and its executor
	$(SECTION)
	@$(RUN) python scripts/operations/local/bootstrap.py install --execute

local-runtime-load: confirm-local ## Load inspected images into the management cluster only
	$(SECTION)
	@$(RUN) python scripts/operations/local/runtime-images.py load-management --execute

local-setup: confirm-local ## Register demo Recipes and config in management
	$(SECTION)
	@$(RUN) python scripts/operations/local/setup-demo.py --execute

local-deploy-management: confirm-local ## Deploy management through Radius; creates no child clusters
	$(SECTION)
	@$(RUN) python scripts/operations/local/deploy-demo.py --execute

local-export: check-work ## Export topology/API access once; exit 3 means not ready
	$(SECTION)
	@$(RUN) python scripts/harness/local/export-state.py --once

local-test: confirm-local ## Run live admissions, isolation checks, and parent outages
	$(SECTION)
	@$(RUN) python scripts/harness/test-e2e.py --config .state/local/acceptance.json --mode all --execute

##@ local Local: cleanup
##! Preview ownership first. Deletion requires CONFIRM_LOCAL=yes.
local-clean-plan: check-work ## Preview verified ownership and ordered demo cleanup
	$(SECTION)
	@$(RUN) python scripts/operations/local/cleanup.py

local-clean: confirm-local ## Delete the verified local demo in Radius ownership order
	$(SECTION)
	@$(RUN) python scripts/operations/local/cleanup.py --execute

local-verify: check-work ## Verify offline; set LOCAL_CLEANUP_RECORD=<record path>
	$(SECTION)
	@test -n "$(LOCAL_CLEANUP_RECORD)" || { echo "Set LOCAL_CLEANUP_RECORD to the exact cleanup record." >&2; exit 1; }
	@$(RUN) python scripts/operations/local/cleanup.py --verify "$(LOCAL_CLEANUP_RECORD)"

require-azure: check-work
	@test "$(ENV)" = azure || { echo "This target is Azure-only; use the explicit local-* targets." >&2; exit 1; }

confirm-azure: require-azure
	@test "$(CONFIRM_AZURE)" = yes || { echo "Set CONFIRM_AZURE=yes for Azure mutations." >&2; exit 1; }

##@ azure Azure: preparation and deployment
##! Use ENV=azure (default). Mutating commands also need CONFIRM_AZURE=yes.
preflight: require-azure ## Inspect prerequisites and write scoped local context
	$(SECTION)
	@$(RUN) python scripts/operations/project.py preflight --environment "$(ENV)"

bootstrap-preview: require-azure ## Prepare foundation inputs and run Azure what-if
	$(SECTION)
	@$(RUN) python scripts/operations/project.py bootstrap-preview --environment "$(ENV)"

validate-azure: require-azure ## Record diagnostics; optionally run what-if/validation
	$(SECTION)
	@$(RUN) python scripts/operations/validate-bootstrap.py

bootstrap: confirm-azure ## Compile and deploy the scoped Azure foundation
	$(SECTION)
	@$(RUN) python scripts/operations/project.py bootstrap --environment "$(ENV)"

install-radius: confirm-azure ## Install management Radius after bootstrap
	$(SECTION)
	@$(RUN) python scripts/operations/project.py install-radius --environment "$(ENV)"

publish-recipes: confirm-azure ## Publish and verify immutable Recipe tags in project ACR
	$(SECTION)
	@$(RUN) python scripts/operations/publish-artifacts.py

build-publish: confirm-azure ## Build/push committed images; inspect contents separately
	$(SECTION)
	@$(RUN) python scripts/operations/build-images.py

register-radius: confirm-azure ## Register cluster types/environment; SLOT defaults to management
	$(SECTION)
	@$(RUN) python scripts/operations/register-radius.py --slot "$(SLOT)" --config "$(CONFIG)"

deploy-management-preview: require-azure ## Prepare the management Job manifest; do not submit it
	$(SECTION)
	@$(RUN) python scripts/operations/run-management-job.py --config "$(CONFIG)"

deploy-management: confirm-azure ## Submit management Job; verify completion separately
	$(SECTION)
	@$(RUN) python scripts/operations/run-management-job.py --config "$(CONFIG)" --execute

##@ azure Azure: acceptance
##! Live tests require CONFIRM_AZURE=yes and the guide's prepared harness state.
export-state: require-azure ## Export protected harness state; exit 3 means not ready
	$(SECTION)
	@$(RUN) python scripts/harness/export-state.py --config "$(CONFIG)" --once

test-e2e: confirm-azure ## Run the opt-in live Azure onboarding scenario
	$(SECTION)
	@$(RUN) python scripts/harness/test-e2e.py --config "$(ACCEPTANCE_CONFIG)" --mode scenario --execute

test-outages: confirm-azure ## Run opt-in live parent-link outages and restoration
	$(SECTION)
	@$(RUN) python scripts/harness/test-e2e.py --config "$(ACCEPTANCE_CONFIG)" --mode outages --execute

##@ azure Azure: cleanup
##! Preview ownership first. Deletion requires ENV=azure CONFIRM_AZURE=yes.
clean-plan: require-azure ## Read cloud ownership and preview ordered cleanup
	$(SECTION)
	@$(RUN) python scripts/operations/clean-azure.py

clean-azure: confirm-azure ## Delete only verified project-owned Azure resources
	$(SECTION)
	@$(RUN) python scripts/operations/clean-azure.py --execute

verify-clean: require-azure ## Verify Azure deletion and report retained recovery records
	$(SECTION)
	@$(RUN) python scripts/operations/verify-clean.py
