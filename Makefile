# radius-todolist-app
#
# One application definition (app.bicep), two environments. The difference
# between "Redis is a pod" and "Redis is Azure Managed Redis" lives entirely in
# environments/*.bicep, never in app.bicep.

include ports.env

KIND_CONTEXT   ?= kind-radius-todolist-app
AKS_CONTEXT    ?= aks-todolist
APP            ?= todolist
RAD_GROUP      ?= todolist
LOCAL_NS       ?= todolist-local-todolist
AZURE_NS       ?= todolist-azure-todolist

SUBSCRIPTION   ?= a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc
LOCATION       ?= swedencentral
PLATFORM_RG    ?= rg-todolist-platform
APP_RG         ?= rg-todolist-app
AKS_NAME       ?= aks-todolist
VNET_NAME      ?= vnet-todolist
PE_SUBNET      ?= snet-privatelink
DNS_ZONE       ?= privatelink.redis.azure.net

.PHONY: help check env-local up-local down-local logs-local test-local \
        infra-azure radius-azure env-azure up-azure down-azure test-azure clean-azure

help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## ---------------------------------------------------------------- validation

check: ## Compile-check every Bicep file (Radius files need the Radius compiler)
	az bicep build --file recipes/azure-managed-redis.bicep --stdout > /dev/null
	az bicep build --file infra/main.bicep --stdout > /dev/null
	rad bicep generate-kubernetes-manifest app.bicep -g $(RAD_GROUP) \
	  --parameters application=/planes/radius/local/resourcegroups/$(RAD_GROUP)/providers/Applications.Core/applications/$(APP) \
	  --parameters environment=/planes/radius/local/resourcegroups/$(RAD_GROUP)/providers/Applications.Core/environments/local \
	  --destination-file /tmp/check-app.yaml
	rad bicep generate-kubernetes-manifest environments/local.bicep -g $(RAD_GROUP) \
	  --destination-file /tmp/check-envlocal.yaml
	@echo "all bicep files compile"

## --------------------------------------------------------------------- local

# rad workspace create validates that the Radius resource group and environment
# already exist, so the workspace is created twice: once with only a context to
# bootstrap, and again once the group and environment exist.
env-local: ## Create the local Radius environment on the kind cluster
	rad workspace create kubernetes local --context $(KIND_CONTEXT) --force
	rad group show $(RAD_GROUP) --workspace local >/dev/null 2>&1 || \
	  rad group create $(RAD_GROUP) --workspace local
	rad deploy environments/local.bicep --workspace local --group $(RAD_GROUP)
	rad workspace create kubernetes local --context $(KIND_CONTEXT) \
	  --group $(RAD_GROUP) --environment local --force

up-local: env-local ## Deploy the application to the kind cluster
	rad deploy app.bicep --application $(APP) --workspace local

down-local: ## Delete the application from the kind cluster
	rad app delete $(APP) --workspace local --yes

logs-local: ## Tail application logs on the kind cluster
	kubectl --context $(KIND_CONTEXT) -n $(LOCAL_NS) logs deploy/demo --tail=50

test-local: ## Prove a todo survives a pod restart on the kind cluster
	./scripts/test-persistence-local.sh

## --------------------------------------------------------------------- azure

infra-azure: ## Create the Azure resource groups, network, DNS zone and AKS cluster
	az group create -n $(PLATFORM_RG) -l $(LOCATION) -o none
	az group create -n $(APP_RG) -l $(LOCATION) -o none
	az deployment group create -g $(PLATFORM_RG) -n todolist-platform \
	  -f infra/main.bicep -p infra/main.bicepparam -o none
	az aks get-credentials -g $(PLATFORM_RG) -n $(AKS_NAME) \
	  --context $(AKS_CONTEXT) --overwrite-existing
	kubelogin convert-kubeconfig -l azurecli
	kubectl --context $(AKS_CONTEXT) get nodes

radius-azure: ## Install Radius on AKS and register its Azure identity
	./scripts/setup-radius-azure.sh

env-azure: ## Create the Azure Radius environment
	./scripts/setup-env-azure.sh

up-azure: ## Deploy the application to AKS
	rad deploy app.bicep --application $(APP) --workspace azure

down-azure: ## Delete the application from AKS
	rad app delete $(APP) --workspace azure --yes

test-azure: ## Read a todo back out of Azure Managed Redis from inside the cluster
	./scripts/test-persistence-azure.sh

clean-azure: ## Delete every Azure resource this project created
	-rad app delete $(APP) --workspace azure --yes
	az group delete -n $(APP_RG) --yes --no-wait
	az group delete -n $(PLATFORM_RG) --yes --no-wait
	@echo "Note: the GHCR package and any fallback service principal survive this."
