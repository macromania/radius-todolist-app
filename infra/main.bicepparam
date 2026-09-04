using 'main.bicep'

// Entra object ID of the human operator. With Azure RBAC enabled and local
// accounts disabled, this assignment is the only way anyone reaches the
// cluster. Get it with: az ad signed-in-user show --query id -o tsv
param operatorObjectId = '0eb018a8-f0af-4e79-ab61-706f5f82def4'

param clusterName = 'aks-todolist'
param vnetName = 'vnet-todolist'
param kubernetesVersion = '1.35'

// Cost note. Four nodes of Standard_D2s_v5 (two system, two user) plus a
// Standard load balancer and Log Analytics ingestion is roughly $280-350 a
// month while this runs. This is a spike: run `make clean-azure` when done.
param nodeVmSize = 'Standard_D2s_v5'
param systemNodeCount = 2
param userNodeMin = 2
param userNodeMax = 4
