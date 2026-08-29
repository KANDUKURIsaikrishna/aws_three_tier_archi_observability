output "grafana_admin_secret_arn" {
  description = "ARN of Secrets Manager secret holding Grafana admin password (/bookstore/grafana-admin). Empty string when create_monitoring_secrets = false (DR region reuses the primary's replica)."
  value       = try(aws_secretsmanager_secret.grafana_admin[0].arn, "")
}

output "monitoring_basic_auth_secret_arn" {
  description = "ARN of Secrets Manager secret holding the shared Prometheus/Alertmanager basic-auth password (/bookstore/monitoring-basic-auth). Empty string when create_monitoring_secrets = false."
  value       = try(aws_secretsmanager_secret.monitoring_basic_auth[0].arn, "")
}
