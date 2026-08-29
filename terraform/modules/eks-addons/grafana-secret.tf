resource "random_password" "grafana_admin" {
  length  = 24
  special = false
}

resource "aws_secretsmanager_secret" "grafana_admin" {
  name                    = "/bookstore/grafana-admin"
  recovery_window_in_days = 0 # 0 = force delete on destroy, no soft-delete window — see TF-012

  # Replicated into the standby region so its monitoring EC2 can fetch the
  # Grafana password locally (var.replica_region, set only when
  # enable_dr_standby is on at the root).
  dynamic "replica" {
    for_each = var.replica_region != "" ? [var.replica_region] : []
    content {
      region = replica.value
    }
  }
}

resource "aws_secretsmanager_secret_version" "grafana_admin" {
  secret_id     = aws_secretsmanager_secret.grafana_admin.id
  secret_string = random_password.grafana_admin.result
}
