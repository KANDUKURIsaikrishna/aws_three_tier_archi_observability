terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
      # Unlike every other module this branch calls twice (network, security,
      # eks, monitoring-ec2 — each swapping its WHOLE default `aws` provider
      # for aws.secondary via `providers = { aws = aws.secondary }` at the
      # call site), route53 is only ever called ONCE at the root and needs
      # BOTH regions' AWS APIs available in that single call: the primary
      # region's ALB hosted zone ID for primary/frontend/api, the secondary
      # region's for `secondary` (var.enable_dr_standby). Hence an explicit
      # second provider alias here instead of the swap-the-whole-module trick.
      configuration_aliases = [aws.secondary]
    }
  }
}
