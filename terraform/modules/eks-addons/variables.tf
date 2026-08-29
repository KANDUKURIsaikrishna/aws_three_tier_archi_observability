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

