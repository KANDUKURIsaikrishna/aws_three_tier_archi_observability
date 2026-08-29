# ── AWS Load Balancer Controller ───────────────────────────────────────────
# Replaces ingress-nginx entirely -- the Helm release that used to install
# it lived in this module's own ingress.tf, now deleted outright rather than
# edited. ingress-nginx (kubernetes/ingress-nginx) was officially retired by
# the Kubernetes project on 2026-03-31: the repo
# is read-only, no further releases, no more CVE patches, ever, for
# something that was directly internet-facing and terminating TLS. Running
# it past that date is a genuine, growing security liability, not a
# style preference -- confirmed via the Kubernetes Steering/Security
# Response Committees' own retirement statement and AWS's own migration
# guidance, which names this exact controller as the replacement.
#
# Provisions a real AWS Application Load Balancer (ALB) per Kubernetes
# Ingress object (or one shared ALB across several, via the
# alb.ingress.kubernetes.io/group.name annotation -- see
# k8s/base/ingress/ingress.yaml and k8s/services/api-gateway/base/ingress.yaml,
# both set to the same group so this project keeps exactly one load balancer,
# not two). Standard IRSA pattern, same shape as external-secrets.tf.

data "aws_iam_policy_document" "aws_lb_controller_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(var.oidc_provider_url, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(var.oidc_provider_url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "aws_lb_controller" {
  name               = "bookstore-aws-lb-controller${var.role_name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.aws_lb_controller_trust.json
}

# Official policy, fetched verbatim from
# kubernetes-sigs/aws-load-balancer-controller's own
# docs/install/iam_policy.json rather than hand-written -- it's long (15
# statements) and easy to get subtly wrong by hand, and AWS periodically
# adds new actions here as the controller gains features (e.g. mutual-TLS
# trust stores, capacity reservations). Re-fetch and diff against that same
# file before bumping the Helm chart version below by anything more than a
# patch release.
resource "aws_iam_role_policy" "aws_lb_controller" {
  name = "bookstore-aws-lb-controller-policy"
  role = aws_iam_role.aws_lb_controller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iam:CreateServiceLinkedRole"]
        Resource = "*"
        Condition = {
          StringEquals = { "iam:AWSServiceName" = "elasticloadbalancing.amazonaws.com" }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeAccountAttributes", "ec2:DescribeAddresses", "ec2:DescribeAvailabilityZones",
          "ec2:DescribeInternetGateways", "ec2:DescribeVpcs", "ec2:DescribeVpcPeeringConnections",
          "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeInstances",
          "ec2:DescribeNetworkInterfaces", "ec2:DescribeTags", "ec2:GetCoipPoolUsage",
          "ec2:DescribeCoipPools", "ec2:GetSecurityGroupsForVpc", "ec2:DescribeIpamPools",
          "ec2:DescribeRouteTables",
          "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeLoadBalancerAttributes",
          "elasticloadbalancing:DescribeListeners", "elasticloadbalancing:DescribeListenerCertificates",
          "elasticloadbalancing:DescribeSSLPolicies", "elasticloadbalancing:DescribeRules",
          "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetGroupAttributes",
          "elasticloadbalancing:DescribeTargetHealth", "elasticloadbalancing:DescribeTags",
          "elasticloadbalancing:DescribeTrustStores", "elasticloadbalancing:DescribeListenerAttributes",
          "elasticloadbalancing:DescribeCapacityReservation",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "cognito-idp:DescribeUserPoolClient", "acm:ListCertificates", "acm:DescribeCertificate",
          "iam:ListServerCertificates", "iam:GetServerCertificate",
          "waf-regional:GetWebACL", "waf-regional:GetWebACLForResource",
          "waf-regional:AssociateWebACL", "waf-regional:DisassociateWebACL",
          "wafv2:GetWebACL", "wafv2:GetWebACLForResource", "wafv2:AssociateWebACL", "wafv2:DisassociateWebACL",
          "shield:GetSubscriptionState", "shield:DescribeProtection", "shield:CreateProtection", "shield:DeleteProtection",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateSecurityGroup"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateTags"]
        Resource = "arn:aws:ec2:*:*:security-group/*"
        Condition = {
          StringEquals = { "ec2:CreateAction" = "CreateSecurityGroup" }
          Null         = { "aws:RequestTag/elbv2.k8s.aws/cluster" = "false" }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "arn:aws:ec2:*:*:security-group/*"
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster"  = "true"
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress", "ec2:DeleteSecurityGroup"]
        Resource = "*"
        Condition = {
          Null = { "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false" }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:CreateTargetGroup"]
        Resource = "*"
        Condition = {
          Null = { "aws:RequestTag/elbv2.k8s.aws/cluster" = "false" }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateListener", "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:CreateRule", "elasticloadbalancing:DeleteRule",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags"]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
        ]
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster"  = "true"
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = ["elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags"]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:listener/net/*/*/*",
          "arn:aws:elasticloadbalancing:*:*:listener/app/*/*/*",
          "arn:aws:elasticloadbalancing:*:*:listener-rule/net/*/*/*",
          "arn:aws:elasticloadbalancing:*:*:listener-rule/app/*/*/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:ModifyLoadBalancerAttributes", "elasticloadbalancing:SetIpAddressType",
          "elasticloadbalancing:SetSecurityGroups", "elasticloadbalancing:SetSubnets",
          "elasticloadbalancing:DeleteLoadBalancer", "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyTargetGroupAttributes", "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:ModifyListenerAttributes", "elasticloadbalancing:ModifyCapacityReservation",
          "elasticloadbalancing:ModifyIpPools",
        ]
        Resource = "*"
        Condition = {
          Null = { "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false" }
        }
      },
      {
        Effect = "Allow"
        Action = ["elasticloadbalancing:AddTags"]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
        ]
        Condition = {
          StringEquals = {
            "elasticloadbalancing:CreateAction" = ["CreateTargetGroup", "CreateLoadBalancer"]
          }
          Null = { "aws:RequestTag/elbv2.k8s.aws/cluster" = "false" }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DeregisterTargets"]
        Resource = "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*"
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:SetWebAcl", "elasticloadbalancing:ModifyListener",
          "elasticloadbalancing:AddListenerCertificates", "elasticloadbalancing:RemoveListenerCertificates",
          "elasticloadbalancing:ModifyRule", "elasticloadbalancing:SetRulePriorities",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "helm_release" "aws_lb_controller" {
  name             = "aws-load-balancer-controller"
  repository       = "https://aws.github.io/eks-charts"
  chart            = "aws-load-balancer-controller"
  version          = "3.4.0"
  namespace        = "kube-system"
  create_namespace = false
  wait             = true
  timeout          = 300

  set {
    name  = "clusterName"
    value = var.cluster_name
  }
  set {
    name  = "region"
    value = var.aws_region
  }
  set {
    name  = "serviceAccount.name"
    value = "aws-load-balancer-controller"
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.aws_lb_controller.arn
  }

  # This `set` block above only references aws_iam_role.aws_lb_controller's
  # ARN, not aws_iam_role_policy.aws_lb_controller (the actual permissions
  # attached to that role) -- so without an explicit depends_on, Terraform
  # has ZERO ordering constraint between them and is free to create this
  # Helm release concurrently with, or even before, the policy attachment
  # finishes. Reproduced live, twice, on two independent apply cycles: the
  # controller pods came up, started reconciling immediately, and hit
  # `UnauthorizedOperation: ec2:DescribeSecurityGroups` on every attempt --
  # `aws iam list-role-policies --role-name bookstore-aws-lb-controller`
  # confirmed zero policies attached at that point. The role existed
  # (nothing failed loudly), it just had no permissions yet when the
  # controller's first reconcile ran, and every subsequent attempt kept
  # failing the same way since the policy still hadn't landed. See
  # TROUBLESHOOTING.md.
  #
  # vpc_cni provides pod networking (the aws-node DaemonSet) -- nothing else
  # in this file references it, so without this Terraform is free to destroy
  # it whenever, including before this release's own uninstall or before
  # null_resource.delete_ingress_objects (below) finishes its cleanup work.
  # Hit exactly that in a real `terraform destroy`: vpc_cni got torn down
  # while the controller was still mid-ALB-deprovision, and although its two
  # already-running pods kept their existing network namespaces (so it
  # didn't crash outright), any NEW pod on any node became unschedulable
  # (aws-cni: "connect: connection refused" to the now-gone CNI socket) --
  # a real risk if the controller pod restarts for any reason mid-cleanup.
  # Forcing this to destroy last (after the controller and its cleanup are
  # both fully done) closes that gap. See TROUBLESHOOTING.md for the incident.
  #
  # (Terraform allows only one depends_on per resource -- both reasons above
  # are combined into this single list.)
  depends_on = [aws_iam_role_policy.aws_lb_controller, aws_eks_addon.vpc_cni]
}

# ── Release the ALB before destroy touches the VPC ─────────────────────────
# Same class of problem ingress-nginx had: an Ingress object with
# ingressClassName=alb makes this controller provision a real ALB as a side
# effect, invisible to Terraform (it's created by the controller reconciling
# an Ingress, not by a Terraform resource). Unlike ingress-nginx's Service-
# based provisioning, the controller adds a finalizer to the Ingress object
# specifically to block its deletion until the real ALB is actually torn
# down -- more reliable than ingress-nginx's teardown ever was, but it still
# needs the Ingress objects deleted before `terraform destroy` gets to the
# VPC, or their finalizers hang the whole destroy waiting on a controller
# that's already gone.
resource "null_resource" "delete_ingress_objects" {
  triggers = {
    cluster_name = var.cluster_name
    region       = var.aws_region
  }

  depends_on = [helm_release.aws_lb_controller]

  # interpreter = ["python3"] makes Terraform invoke python3 DIRECTLY, no
  # cmd.exe (Windows) or /bin/sh (POSIX) in between -- sidesteps shell-
  # quoting entirely rather than working around it. Ported faithfully to
  # scripts/delete_ingress_objects.py, preserving the exact
  # fail-loudly-on-kubectl-delete vs. best-effort-on-security-group-poll
  # semantics documented there (and see that script's own comments for the
  # incidents each step was written to prevent: silently-swallowed ALB
  # deletion timeouts orphaning the real ALB, and the controller's shared
  # backend security group outliving its Ingress objects and blocking
  # DeleteVpc).
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["python3"]
    environment = {
      CLUSTER_NAME = self.triggers.cluster_name
      REGION       = self.triggers.region
      # Per-region kubeconfig so this destroy-time `update-kubeconfig` +
      # `kubectl delete` can't race the other region's copy of this same
      # module (dr-standby.tf's module.eks_addons_dr) over a shared
      # ~/.kube/config when a two-region `terraform destroy` runs them at
      # once -- otherwise one region's kubectl could delete Ingress objects
      # in the wrong cluster.
      KUBECONFIG = "${path.module}/../../.terraform/kubeconfig-ingress-${self.triggers.region}"
    }
    command = "${path.module}/../../../scripts/delete_ingress_objects.py"
  }
}
