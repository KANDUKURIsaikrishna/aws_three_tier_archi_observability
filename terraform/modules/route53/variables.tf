variable "vpc_id" {
  description = "VPC ID for the private hosted zone association"
  type        = string
}

variable "rds_private_zone_name" {
  description = "Name of the private Route 53 hosted zone for internal RDS DNS"
  type        = string
  default     = "bookstore.internal"
}

variable "rds_record_name" {
  description = "DNS record name for the RDS endpoint within the private zone"
  type        = string
  default     = "db.bookstore.internal"
}

variable "rds_endpoint" {
  description = "RDS instance endpoint to create a CNAME record for"
  type        = string
}

variable "domain" {
  description = "Apex domain for the public hosted zone and failover records (e.g. example.com)"
  type        = string
}

variable "primary_alb_dns" {
  description = "Nginx NLB DNS in primary region. Leave empty to skip app records before EKS is ready."
  type        = string
  default     = ""
}

variable "secondary_alb_dns" {
  description = "Nginx NLB DNS in secondary region. Fill after secondary EKS deploy."
  type        = string
  default     = ""
}

variable "create_secondary_record" {
  description = <<-EOT
    Gates aws_route53_record.secondary's count. Deliberately separate from
    checking secondary_alb_dns != "" directly: on a var.enable_dr_standby
    apply, secondary_alb_dns is fed from a value only known partway through
    THIS SAME apply (the DR ALB's discovered hostname, see dr-standby.tf's
    dr_discovered_alb_dns) -- an unknown-until-apply string can't decide a
    resource's count (Terraform must resolve count at plan time), so a
    "!= \"\"" check on that value fails plan outright with "Invalid count
    argument". The caller passes a statically-known bool instead
    (var.enable_dr_standby || var.secondary_alb_dns != "" at the root, both
    plain vars with no resource/data dependency) -- the record's VALUE
    (secondary_alb_dns) can still be legitimately unknown at plan time and
    only resolved during apply; only count needs to be plan-time-static.
  EOT
  type        = bool
  default     = false
}

variable "enable_cloudfront" {
  description = "When true, the primary record points at cloudfront_domain instead of primary_alb_dns."
  type        = bool
  default     = false
}

variable "cloudfront_domain" {
  description = "CloudFront distribution domain name. Required when enable_cloudfront=true."
  type        = string
  default     = ""
}
