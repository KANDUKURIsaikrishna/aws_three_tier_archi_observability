# ─────────────────────────────────────────────────────────────────────────────
# Aliased helm / kubernetes / kubectl providers for the standby-region EKS
# cluster (module.eks_dr in dr-standby.tf).
#
# All three read their connection details from module.eks_dr[*] via one(...).
# When var.enable_dr_standby is false, module.eks_dr has zero instances,
# one(...) is null, and try(...) falls back to "" — the providers stay
# unconfigured and unused (every DR resource is count-gated to 0), so this is
# a no-op on a single-region apply.
#
# The aws.secondary provider itself lives in providers.tf (already there,
# configured unconditionally from var.secondary_region).
# ─────────────────────────────────────────────────────────────────────────────

provider "helm" {
  alias = "secondary"

  # Separate repo cache/config from the primary provider's so the two don't
  # race on the same files (see the primary provider's comment in providers.tf).
  repository_config_path = "${path.module}/.terraform/helm/repositories-dr.yaml"
  repository_cache       = "${path.module}/.terraform/helm/cache-dr"

  kubernetes {
    host                   = try(one(module.eks_dr[*].cluster_endpoint), "")
    cluster_ca_certificate = try(base64decode(one(module.eks_dr[*].cluster_ca_certificate)), "")
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", try(one(module.eks_dr[*].cluster_name), ""), "--region", var.secondary_region]
    }
  }
}

provider "kubernetes" {
  alias                  = "secondary"
  host                   = try(one(module.eks_dr[*].cluster_endpoint), "")
  cluster_ca_certificate = try(base64decode(one(module.eks_dr[*].cluster_ca_certificate)), "")
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", try(one(module.eks_dr[*].cluster_name), ""), "--region", var.secondary_region]
  }
}

provider "kubectl" {
  alias                  = "secondary"
  host                   = try(one(module.eks_dr[*].cluster_endpoint), "")
  cluster_ca_certificate = try(base64decode(one(module.eks_dr[*].cluster_ca_certificate)), "")
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", try(one(module.eks_dr[*].cluster_name), ""), "--region", var.secondary_region]
  }
}
