variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR block, e.g. 10.0.0.0/16."
  }
}

variable "public_subnets" {
  description = "List of public subnets with CIDR and Availability Zone"
  type = list(object({
    cidr = string
    az   = string
  }))

  validation {
    condition     = alltrue([for s in var.public_subnets : can(cidrhost(s.cidr, 0))])
    error_message = "Every public_subnets entry's cidr must be a valid CIDR block."
  }
}

variable "private_subnets" {
  description = "List of private subnets with CIDR and Availability Zone"
  type = list(object({
    cidr = string
    az   = string
  }))

  validation {
    condition     = alltrue([for s in var.private_subnets : can(cidrhost(s.cidr, 0))])
    error_message = "Every private_subnets entry's cidr must be a valid CIDR block."
  }
}

variable "single_nat_gateway" {
  description = <<-EOT
    true  = one NAT Gateway shared by every private subnet (cheaper, but a
            single-AZ SPOF for all outbound traffic from private subnets).
    false = one NAT Gateway per AZ, each private subnet routed through the NAT
            in its own AZ (survives losing an AZ; roughly one extra NAT's
            hourly + per-GB cost per additional AZ).
    NAT Gateways are a managed service and do not consume the account's EC2
    vCPU quota, so per-AZ is available regardless of that limit.
  EOT
  type        = bool
  default     = false
}
