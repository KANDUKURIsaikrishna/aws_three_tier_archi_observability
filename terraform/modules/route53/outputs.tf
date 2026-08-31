output "public_zone_id" {
  description = "Route53 public hosted zone ID — created once via scripts/init_domain.py, looked up here"
  value       = data.aws_route53_zone.public.zone_id
}

output "public_name_servers" {
  description = "NS records already set at your domain registrar by scripts/init_domain.py — informational only"
  value       = data.aws_route53_zone.public.name_servers
}
