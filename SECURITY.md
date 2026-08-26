# Security Policy

This is a personal reference/demo project, not a production service handling real user data — but real security practices matter here regardless, and reports are taken seriously.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security vulnerability. Instead, use GitHub's private vulnerability reporting: go to the **Security** tab of this repository → **Report a vulnerability**.

Include, where possible:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Which component is affected (Terraform/AWS infra, a specific microservice, the CI/CD pipeline, k8s manifests, etc.)

## What's already covered

Before reporting, check whether the issue is already addressed by one of this project's existing controls (see [`README.md`](README.md#security-controls)):
- Gitleaks (secret scanning, full git history) on every push
- SonarCloud (SAST, code smells, security hotspots)
- Trivy (container CVE scanning, blocks on CRITICAL/HIGH)
- tfsec (Terraform IaC scanning)
- GitHub OIDC → IAM role assumption (no static AWS keys anywhere)
- External Secrets Operator + AWS Secrets Manager (no secrets in git or k8s manifests)
- Non-root containers, read-only root filesystems, Kubernetes NetworkPolicy segmentation

## Scope

This covers the code and Terraform/Kubernetes configuration in this repository. It does not cover any live deployment a fork or clone of this project might be running — that deployment's security is the operator's responsibility.
