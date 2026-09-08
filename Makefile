# radius-todolist-app
#
# One application definition (infra/radius/app.bicep), two environments.
# The difference between "Redis is a pod" and "Redis is Azure Managed Redis"
# lives in infra/radius/environments/*.bicep, never in the application definition.
#
# Run `make help` for the target list.

include ports.env

KIND_CONTEXT   ?= kind-radius-todolist-app
AKS_CONTEXT    ?= aks-todolist
APP            ?= todolist
RAD_GROUP      ?= todolist
LOCAL_NS       ?= todolist-local-todolist
AZURE_NS       ?= todolist-azure-todolist

SUBSCRIPTION   ?= a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc
TENANT         ?= 16b3c013-d300-468d-ac64-7eda0820b6d3
LOCATION       ?= eastus2
PLATFORM_RG    ?= rg-todolist-platform
APP_RG         ?= rg-todolist-app
AKS_NAME       ?= aks-todolist

ACR_NAME       ?= acrtodolistjts7g6kk6ua66

# Radius 0.60 rejects digest references for Recipes and demands a tag, even
# though `rad bicep publish` prints a digest URL and calls it the way to pin the
# artifact immutably. Immutability is therefore enforced two other ways: the tag
# is locked in the registry (writeEnabled=false, deleteEnabled=false), and
# scripts/setup-env-azure.sh refuses to deploy unless the tag still resolves to
# the digest below. For Recipe changes, publish a new RECIPE_TAG, then update
# the tag and digest below. Reruns reuse an existing tag only if its digest matches.
RECIPE_TAG     ?= 0.1.0
RECIPE_REF     ?= $(ACR_NAME).azurecr.io/radius-recipes/azure-managed-redis:$(RECIPE_TAG)
RECIPE_EXPECTED_DIGEST ?= sha256:fa1f09dc9b1ceb21faac753a2360855f6d1689de92acc8a06a4b9cb0ba07417e

# az bicep is too old to compile two of these files: it lacks the redisEnterprise
# types and rejects @secure() outputs with BCP129. The compiler bundled with rad
# does both, so use it for everything.
BICEP          ?= $(HOME)/.rad/bin/bicep

.PHONY: help check test-publish-recipe env-local up-local down-local logs-local test-local \
        registry-azure publish-recipe infra-azure radius-azure env-azure \
        up-azure down-azure logs-azure test-azure clean-azure

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## ---------------------------------------------------------------- validation

check: ## Compile-check every Bicep file
	$(BICEP) build --stdout infra/radius/recipes/azure/managed-redis.bicep > /dev/null
	$(BICEP) build --stdout infra/main.bicep > /dev/null
	$(BICEP) build --stdout infra/registry.bicep > /dev/null
	rad bicep generate-kubernetes-manifest infra/radius/app.bicep -g $(RAD_GROUP) \
	  --parameters application=/planes/radius/local/resourcegroups/$(RAD_GROUP)/providers/Applications.Core/applications/$(APP) \
	  --parameters environment=/planes/radius/local/resourcegroups/$(RAD_GROUP)/providers/Applications.Core/environments/local \
	  --destination-file /tmp/check-app.yaml
	rad bicep generate-kubernetes-manifest infra/radius/environments/local.bicep -g $(RAD_GROUP) \
	  --destination-file /tmp/check-envlocal.yaml
	rad bicep generate-kubernetes-manifest infra/radius/environments/azure.bicep -g $(RAD_GROUP) \
	  --parameters azureSubscriptionId=$(SUBSCRIPTION) \
	  --parameters redisRecipeRef=$(RECIPE_REF) \
	  --parameters privateEndpointSubnetId=/subscriptions/x/resourceGroups/y/providers/Microsoft.Network/virtualNetworks/v/subnets/s \
	  --parameters privateDnsZoneId=/subscriptions/x/resourceGroups/y/providers/Microsoft.Network/privateDnsZones/z \
	  --destination-file /tmp/check-envazure.yaml
	@echo "all bicep files compile"

test-publish-recipe: ## Test Recipe publishing without Azure access
	bash scripts/test-publish-recipe.sh

## --------------------------------------------------------------------- local

# rad workspace create validates that the Radius resource group and environment
# already exist, so the workspace is created twice: once with only a context to
# bootstrap, and again once both exist.
env-local: ## Create the local Radius environment on the kind cluster
	rad workspace create kubernetes local --context $(KIND_CONTEXT) --force
	rad group show $(RAD_GROUP) --workspace local >/dev/null 2>&1 || \
	  rad group create $(RAD_GROUP) --workspace local
	rad deploy infra/radius/environments/local.bicep --workspace local --group $(RAD_GROUP)
	rad workspace create kubernetes local --context $(KIND_CONTEXT) \
	  --group $(RAD_GROUP) --environment local --force

up-local: env-local ## Deploy the application to the kind cluster
	rad deploy infra/radius/app.bicep --application $(APP) --workspace local

down-local: ## Delete the application from the kind cluster
	rad app delete $(APP) --workspace local --yes

logs-local: ## Tail application logs on the kind cluster
	kubectl --context $(KIND_CONTEXT) -n $(LOCAL_NS) logs deploy/demo --tail=50

test-local: ## Prove a todo survives a pod restart on the kind cluster
	./scripts/test-persistence-local.sh

## --------------------------------------------------------------------- azure

registry-azure: ## Create the resource groups and the Recipe registry
	az group create -n $(PLATFORM_RG) -l $(LOCATION) -o none
	az group create -n $(APP_RG) -l $(LOCATION) -o none
	az deployment group create -g $(PLATFORM_RG) -n registry \
	  -f infra/registry.bicep -o none

publish-recipe: ## Publish or reuse the pinned Recipe, then lock the tag
	ACR_NAME="$(ACR_NAME)" SUBSCRIPTION="$(SUBSCRIPTION)" \
	  RECIPE_TAG="$(RECIPE_TAG)" RECIPE_EXPECTED_DIGEST="$(RECIPE_EXPECTED_DIGEST)" \
	  bash scripts/publish-recipe.sh

infra-azure: ## Create the network, private DNS zone and AKS cluster
	az deployment group create -g $(PLATFORM_RG) -n todolist-platform \
	  -f infra/main.bicep -p infra/main.bicepparam -o none
	az aks get-credentials -g $(PLATFORM_RG) -n $(AKS_NAME) \
	  --context $(AKS_CONTEXT) --overwrite-existing
	kubelogin convert-kubeconfig -l azurecli
	kubectl --context $(AKS_CONTEXT) get nodes

radius-azure: ## Install Radius on AKS and register its Azure identity
	./scripts/setup-radius-azure.sh

env-azure: ## Create the Azure Radius environment
	RECIPE_REF=$(RECIPE_REF) RECIPE_EXPECTED_DIGEST=$(RECIPE_EXPECTED_DIGEST) \
	  ACR_NAME=$(ACR_NAME) ./scripts/setup-env-azure.sh

up-azure: ## Deploy the application to AKS
	rad deploy infra/radius/app.bicep --application $(APP) --workspace azure

down-azure: ## Delete the application from AKS
	rad app delete $(APP) --workspace azure --yes

logs-azure: ## Tail application logs on AKS
	kubectl --context $(AKS_CONTEXT) -n $(AZURE_NS) logs deploy/demo --tail=50

test-azure: ## Read a todo back out of Azure Managed Redis from inside the cluster
	./scripts/test-persistence-azure.sh

clean-azure: ## Delete every Azure resource this project created
	-rad app delete $(APP) --workspace azure --yes
	az group delete -n $(APP_RG) --yes --no-wait
	az group delete -n $(PLATFORM_RG) --yes --no-wait
	@echo "Note: the fallback service principal, if one was created, survives this."
