variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the EKS cluster OIDC provider — used for IRSA role creation"
  type        = string
}

variable "node_role_name" {
  description = "IAM role name of the EKS node group — receives the EBS CSI driver policy"
  type        = string
}

variable "oidc_provider_url" {
  description = "URL of the EKS cluster OIDC provider, WITH the https:// scheme — stripped via replace() at point of use in IRSA trust policy conditions"
  type        = string
}

variable "aws_region" {
  description = "AWS region — used to scope the external-secrets IRSA policy to this account/region"
  type        = string
}

variable "replica_region" {
  description = "If non-empty, the grafana-admin and monitoring-basic-auth Secrets Manager entries get a cross-region replica here. Set by the root only when var.enable_dr_standby is on. Empty = no replica (default)."
  type        = string
  default     = ""
}

variable "create_monitoring_secrets" {
  description = "Create the /bookstore/grafana-admin and /bookstore/monitoring-basic-auth Secrets Manager entries (default). Set false in the DR-region instantiation, where those secrets already exist as cross-region replicas of the primary's -- creating them again would collide on the name."
  type        = bool
  default     = true
}

variable "role_name_suffix" {
  description = <<-EOT
    Appended to the aws-lb-controller and external-secrets IAM role names
    (e.g. "bookstore-aws-lb-controller$${suffix}"). Empty by default (primary
    region). Unlike create_monitoring_secrets/replica_region above, these two
    IAM roles have no cross-region-replica option -- IAM is account-global,
    not region-scoped, so a second same-named module.eks_addons instantiation
    (var.enable_dr_standby's module.eks_addons_dr) collides outright with
    IAM: EntityAlreadyExists on CreateRole, discovered on this branch's first
    real two-region apply. Set to "-dr" (or similar) for that instantiation
    instead of trying to share/replicate the primary's role the way the
    monitoring secrets do.
  EOT
  type        = string
  default     = ""
}

