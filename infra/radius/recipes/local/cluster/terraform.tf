terraform {
  required_version = ">= 1.14, < 1.16"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "= 2.3.5"
    }
    kind = {
      source  = "tehcyx/kind"
      version = "= 0.11.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "= 2.38.0"
    }
  }
}
