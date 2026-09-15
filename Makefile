SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV ?= azure
ARGS ?=
GROUP ?= all
COLOR ?= auto
BICEP ?= $(HOME)/.rad/bin/bicep
RUN := uv run --no-sync
STAGE := bash scripts/operations/stage.sh
SEPARATOR := ------------------------------------------------------------------------------
export CONFIRM_AZURE CONFIRM_LOCAL
export PYTHONDONTWRITEBYTECODE := 1
CHECK_TMP := $(CURDIR)/.state/check/tmp
check lint test check-bicep check-terraform: export TMPDIR := $(CHECK_TMP)

define TERMINAL_STYLE
case "$(COLOR)" in \
  auto|always|never) ;; \
  *) printf 'Invalid COLOR. Use COLOR=auto, always, or never.\n' >&2; exit 2 ;; \
esac; \
bold=; accent=; reset=; \
if [ -z "$${NO_COLOR:-}" ] && { [ "$(COLOR)" = always ] || \
  { [ "$(COLOR)" = auto ] && [ -t $(1) ] && [ "$${TERM:-dumb}" != dumb ]; }; }; then \
  bold=$$(printf '\033[1m'); \
  accent=$$(printf '\033[34m'); \
  reset=$$(printf '\033[0m'); \
fi;
endef

define SECTION
@$(call TERMINAL_STYLE,2) \
printf '\n\n%s== %s ==%s\n%s%s%s\n\n' \
  "$$bold$$accent" '$@' "$$reset" "$$accent" "$(SEPARATOR)" "$$reset" >&2
endef

.PHONY: help init show-config endpoints api kube fault check lint test test-integration check-bicep check-shell check-terraform check-work \
        require-azure confirm-azure build inspect-build bootstrap deploy-management \
        deploy-management-preview export-state test-e2e test-outages clean-plan clean clean-azure verify-clean \
        confirm-local local-build local-inspect-build local-bootstrap \
        local-setup local-deploy-management local-export local-test local-clean-plan \
        local-clean local-verify

##@ setup Setup and API
##! Choose ENV when initializing .env; pass only nonsecret options through ARGS.
help: ## Show grouped commands; use GROUP=setup, checks, local, or azure
	@$(call TERMINAL_STYLE,1) \
	awk -v group="$(GROUP)" -v bold="$$bold" -v accent="$$accent" -v reset="$$reset" \
	  -v rule="$(SEPARATOR)" '\
	  function heading(title) { \
	    printf "\n\n%s%s%s\n%s%s%s\n\n", bold accent, title, reset, accent, rule, reset; \
	  } \
	  function example(label, command) { \
	    printf "  %-16s  %s%s%s\n", label, bold, command, reset; \
	  } \
	  function info_heading(title) { \
	    printf "\n\n  %s[info] %s%s\n\n", bold, title, reset; \
	  } \
	  function info_row(label, text) { \
	    printf "    %-16s  %s\n", label, text; \
	  } \
	  BEGIN { \
	    if (group !~ /^(all|setup|checks|local|azure)$$/) { \
	      print "Unknown help group. Use GROUP=all, setup, checks, local, or azure." > "/dev/stderr"; \
	      exit 2; \
	    } \
	    heading("Radius three-plane demo"); \
	    example("Usage:", "make <target> [NAME=value]"); \
	    example("Focus:", "make help GROUP=azure"); \
	    print "\n  Groups: setup, checks, local, azure, all (default)"; \
	    print "  Style:  COLOR=auto (default), always, never; NO_COLOR=1 disables styling"; \
	  } \
	  /^##@ / { \
	    show = (group == "all" || group == $$2); \
	    if (show) { sub(/^##@ [^ ]+ /, ""); heading($$0); } \
	    next; \
	  } \
	  /^##! / && show { printf "  %s\n\n", substr($$0, 5); next; } \
	  /^[a-zA-Z0-9_-]+:.*## / && show { \
	    split($$0, entry, ":.*## "); \
	    printf "  %s%-28s%s  %s\n", bold, entry[1], reset, entry[2]; \
	  } \
	  END { \
	    if (group ~ /^(all|setup|checks|local|azure)$$/) { \
	      info_heading("Start here"); \
	      info_row("Check source:", "make check"); \
	      if (group != "checks") { \
	        if (group != "local") info_row("Configure Azure:", "make init ENV=azure"); \
	        if (group != "azure") info_row("Configure local:", "make init ENV=local"); \
	        info_row("Find endpoints:", "make endpoints ARGS=all"); \
	        info_row("Call the API:", "make api ARGS='\''management GET /tenants/alpha'\''"); \
	        info_heading("Walkthroughs"); \
	        if (group != "local") info_row("Azure:", "RUN_AZURE_SCENARIOS.md"); \
	        if (group != "azure") info_row("Local:", "RUN_LOCAL_SCENARIOS.md"); \
	        print "\n    Follow the selected guide for prerequisites and manual scenarios."; \
	      } else { \
	        info_heading("Read next"); \
	        info_row("Dependency setup:", "README.md#checks"); \
	      } \
	      print ""; \
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

kube: ## Run kubectl with fresh access; ARGS='SLOT get pods'
	$(SECTION)
	@bash scripts/operations/kube.sh $(ARGS)

fault: ## Run or restore a parent-link fault; pass helper options through ARGS
	$(SECTION)
	@$(STAGE) fault $(ARGS)

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

confirm-local:
	@test "$(CONFIRM_LOCAL)" = yes || { echo "Set CONFIRM_LOCAL=yes for local Docker/Kubernetes mutations." >&2; exit 1; }

##@ local Local: preparation and images
##! Use Docker Desktop. Build before bootstrap; .env selects the deployment.
local-build: ## Build and inspect all local images and prepared dependencies
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) build

local-inspect-build: ## Reinspect local image bytes without building or pulling
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) inspect-build

##@ local Local: deployment and acceptance
##! Mutating commands require CONFIRM_LOCAL=yes. Run stages from the local guide.
local-bootstrap: ## Create management kind, node-owned encryption and Radius
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) bootstrap

local-setup: ## Register prepared Recipes separately for a manual checkpoint
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) setup

local-deploy-management: ## Register Recipes and deploy management through Radius
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) deploy-management

local-export: ## Report live local topology/API access; no saved export is required
	$(SECTION)
	@$(RUN) python scripts/harness/local/export-state.py --once

local-test: confirm-local ## Run live admissions, isolation checks, and parent outages
	$(SECTION)
	@$(RUN) python scripts/harness/test-e2e.py --environment local --mode all --execute

##@ local Local: cleanup
##! Preview ownership first. Deletion requires CONFIRM_LOCAL=yes.
local-clean-plan: ## Preview live ownership and ordered local cleanup
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) clean-plan

local-clean: ## Delete the verified local demo in Radius ownership order
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) clean

local-verify: ## Verify live local absence; no record path is required
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=local $(STAGE) verify-clean

require-azure:
	@test "$(ENV)" = azure || { echo "This target is Azure-only; use the explicit local-* targets." >&2; exit 1; }

confirm-azure: require-azure
	@test "$(CONFIRM_AZURE)" = yes || { echo "Set CONFIRM_AZURE=yes for Azure mutations." >&2; exit 1; }

##@ azure Azure-first workflow: selected environment stages
##! .env selects Azure or local. Mutations need CONFIRM_AZURE=yes or CONFIRM_LOCAL=yes.
build: ## Build and inspect selected images, Recipes and local dependencies
	$(SECTION)
	@$(STAGE) build

inspect-build: ## Reinspect selected image contents and artifact ownership
	$(SECTION)
	@$(STAGE) inspect-build

bootstrap: ## Deploy the selected foundation and install management Radius
	$(SECTION)
	@$(STAGE) bootstrap

deploy-management-preview: ## Inspect management deployment inputs without submission
	$(SECTION)
	@$(STAGE) preview-management

deploy-management: ## Deploy management and wait for actual completion
	$(SECTION)
	@$(STAGE) deploy-management

##@ azure Azure: acceptance
##! Live tests require CONFIRM_AZURE=yes and a matching deployed .env selection.
export-state: require-azure ## Report live Azure topology/API access; no saved export is required
	$(SECTION)
	@$(RUN) python scripts/harness/export-state.py --environment azure --once

test-e2e: confirm-azure ## Run the opt-in live Azure onboarding scenario
	$(SECTION)
	@$(RUN) python scripts/harness/test-e2e.py --environment azure --mode scenario --execute

test-outages: confirm-azure ## Run opt-in live parent-link outages and restoration
	$(SECTION)
	@$(RUN) python scripts/harness/test-e2e.py --environment azure --mode outages --execute

##@ azure Selected environment: cleanup
##! Preview ownership first. Confirm deletion for the environment selected in .env.
clean-plan: ## Read current ownership and preview ordered cleanup
	$(SECTION)
	@$(STAGE) clean-plan

clean: ## Delete the selected demo in Radius ownership order
	$(SECTION)
	@$(STAGE) clean

clean-azure: ## Delete only the Azure deployment selected in .env
	$(SECTION)
	@PLANE_DEMO_EXPECT_ENV=azure $(STAGE) clean

verify-clean: ## Verify live absence; no saved cleanup record is required
	$(SECTION)
	@$(STAGE) verify-clean
