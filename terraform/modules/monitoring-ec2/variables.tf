variable "vpc_id" {
  description = "VPC where the monitoring EC2 instance is deployed"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block — allows Fluent Bit on EKS nodes to push logs to Loki"
  type        = string
}

variable "public_subnet_id" {
  description = "Public subnet for the monitoring EC2 instance"
  type        = string
}

variable "eip_allocation_id" {
  description = "Elastic IP allocation ID to associate with the monitoring instance"
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name — used to discover node IPs at boot"
  type        = string
}

variable "region" {
  description = "AWS region"
  type        = string
}

variable "eks_cluster_sg_id" {
  description = "EKS cluster security group ID — monitoring EC2 gets inbound rule to scrape node-exporter on port 9100"
  type        = string
}

variable "eks_api_server" {
  description = "EKS cluster API server endpoint URL (e.g. https://XXXX.gr7.<region>.eks.amazonaws.com) — used by Prometheus's kubernetes_sd_configs to discover pods and scrape app-level /metrics via the API server's pod-proxy, since pod IPs aren't reachable from outside the cluster network. Templated, not hardcoded, so it doesn't go stale like OBS-032's literal IP did — a fresh endpoint every apply is picked up automatically."
  type        = string
}

variable "grafana_admin_secret_arn" {
  description = "Secrets Manager secret ARN for Grafana admin password — EC2 IAM policy allows GetSecretValue on this ARN"
  type        = string
}

variable "grafana_admin_secret_name" {
  description = "Secrets Manager secret name (path) for Grafana admin password"
  type        = string
  default     = "/bookstore/grafana-admin"
}

variable "monitoring_basic_auth_secret_arn" {
  description = "Secrets Manager secret ARN for the shared Prometheus/Alertmanager basic-auth password — EC2 IAM policy allows GetSecretValue on this ARN"
  type        = string
}

variable "monitoring_basic_auth_secret_name" {
  description = "Secrets Manager secret name (path) for the shared Prometheus/Alertmanager basic-auth password"
  type        = string
  default     = "/bookstore/monitoring-basic-auth"
}

variable "alertmanager_smtp_secret_arn" {
  description = "Secrets Manager secret ARN for Alertmanager's SES SMTP credentials (JSON: SMTP_HOST/PORT/USERNAME/PASSWORD/FROM/TO) — EC2 IAM policy allows GetSecretValue on this ARN"
  type        = string
}

variable "alertmanager_smtp_secret_name" {
  description = "Secrets Manager secret name (path) for Alertmanager's SES SMTP credentials"
  type        = string
}

variable "admin_cidr_blocks" {
  description = "CIDRs allowed to access Grafana (3000) and Prometheus (9090)"
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = alltrue([for c in var.admin_cidr_blocks : can(cidrhost(c, 0))])
    error_message = "admin_cidr_blocks must be a list of valid CIDR blocks."
  }
}

variable "instance_type" {
  description = "EC2 instance type for the monitoring server"
  type        = string
  default     = "t3.small"
}

variable "role_name_suffix" {
  description = <<-EOT
    Appended to the aws_iam_role.monitoring and aws_iam_instance_profile.monitoring
    names (e.g. "bookstore-monitoring-ec2$${suffix}"). Empty by default (primary
    region). IAM is account-global, not region-scoped -- var.enable_dr_standby's
    module.monitoring_ec2_dr instantiating this module a second time collides
    outright on both the role AND the instance profile (both IAM resources)
    without this, discovered on the dr branch's first real two-region apply
    (same class of bug as modules/eks-addons's own role_name_suffix, and the
    same fix). Everything else in this module (SG name, key pair name) is
    region-scoped, not global, and needs no suffix.
  EOT
  type        = string
  default     = ""
}
