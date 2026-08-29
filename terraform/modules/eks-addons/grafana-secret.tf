resource "random_password" "grafana_admin" {
  count   = var.create_monitoring_secrets ? 1 : 0
  length  = 24
  special = false
}

resource "aws_secretsmanager_secret" "grafana_admin" {
  count                   = var.create_monitoring_secrets ? 1 : 0
  name                    = "/bookstore/grafana-admin"
  recovery_window_in_days = 0 # 0 = force delete on destroy, no soft-delete window — see TF-012

  # Replicated into the standby region so its monitoring EC2 can fetch the
  # Grafana password locally (var.replica_region, set only when
  # enable_dr_standby is on at the root). The DR-region eks-addons runs with
  # create_monitoring_secrets = false and consumes this replica instead of
  # creating its own.
  dynamic "replica" {
    for_each = var.replica_region != "" ? [var.replica_region] : []
    content {
      region = replica.value
    }
  }
}

resource "aws_secretsmanager_secret_version" "grafana_admin" {
  count         = var.create_monitoring_secrets ? 1 : 0
  secret_id     = aws_secretsmanager_secret.grafana_admin[0].id
  secret_string = random_password.grafana_admin[0].result
}
