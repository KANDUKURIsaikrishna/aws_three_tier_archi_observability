data "aws_caller_identity" "current" {}

# Resolves the first two available AZs in whatever region is actually
# configured (var.aws_region) -- locals.tf's subnet map used to hardcode
# "us-west-1a"/"us-west-1c" literally, which broke apply outright in any
# other region (those AZ names don't exist there).
data "aws_availability_zones" "available" {
  state = "available"
}
