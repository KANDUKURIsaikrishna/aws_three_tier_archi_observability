provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "bookstore"
      Environment = var.environment
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}

provider "aws" {
  alias  = "secondary"
  region = var.secondary_region

  default_tags {
    tags = {
      Project     = "bookstore"
      Environment = var.environment
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}

# CloudFront ACM certs must be in us-east-1 — AWS hard requirement, regardless of secondary DR region.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "bookstore"
      Environment = var.environment
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}

provider "helm" {
  # Isolates this project's Helm repo state from whatever else is registered
  # in the operator's global Helm config (%APPDATA%\helm\repositories.yaml
  # on Windows, ~/.config/helm/repositories.yaml elsewhere). Without this,
  # the provider refreshes the index for EVERY repo in that global config on
  # every apply, not just the repos this project actually declares -- one
  # stale/unreachable entry left over from an unrelated project (e.g. a
  # removed cert-manager install's jetstack repo) cascade-fails every
  # helm_release resource here, not just whichever one happens to share
  # that repo. .terraform/ is already gitignored, so this doesn't add
  # anything to version control.
  repository_config_path = "${path.module}/.terraform/helm/repositories.yaml"
  repository_cache       = "${path.module}/.terraform/helm/cache"

  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
    }
  }
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
  }
}

provider "kubectl" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
  }
}
