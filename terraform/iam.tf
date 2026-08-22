# GitHub Actions OIDC Role
# Lets GitHub Actions assume an AWS role via OIDC — no static AWS keys needed.
#
# Prerequisite (one-time per AWS account, outside Terraform):
#   aws iam create-open-id-connect-provider \
#     --url https://token.actions.githubusercontent.com \
#     --client-id-list sts.amazonaws.com \
#     --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# data.aws_caller_identity.current is defined in data.tf

resource "aws_iam_role" "github_oidc" {
  name = "bookstore-github-oidc-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Wildcards after the owner and repo names, not just a literal
          # "repo:${var.github_repo}:..." match -- GitHub's OIDC token `sub`
          # claim isn't always the classic "repo:OWNER/REPO:ref:..." shape.
          # Confirmed via CloudTrail on a real push: some accounts' tokens
          # come through as "repo:OWNER@ownerId/REPO@repoId:ref:..." (numeric
          # GitHub IDs appended to both names), which a literal match rejects
          # outright with "Not authorized to perform sts:AssumeRoleWithWebIdentity"
          # -- no indication anywhere *why*, since IAM trust-policy denials
          # don't include the claim it actually received. The wildcards below
          # match either shape (an exact literal name plus zero extra
          # characters, or the same name plus "@<id>") without loosening which
          # owner/repo can assume this role -- the prefix up to each wildcard
          # still has to match exactly.
          "token.actions.githubusercontent.com:sub" = [
            "repo:${split("/", var.github_repo)[0]}*/${split("/", var.github_repo)[1]}*:ref:refs/heads/main",
            "repo:${split("/", var.github_repo)[0]}*/${split("/", var.github_repo)[1]}*:ref:refs/heads/improvements",
            # ci-cd.yml's build-and-push job runs on observability too (see
            # CICD.md), but this trust policy was never updated to match —
            # confirmed live: images could never actually push from this
            # branch until this line existed.
            "repo:${split("/", var.github_repo)[0]}*/${split("/", var.github_repo)[1]}*:ref:refs/heads/observability",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_oidc_ecr" {
  name = "ecr-push"
  role = aws_iam_role.github_oidc.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/bookstore-*"
      },
    ]
  })
}
