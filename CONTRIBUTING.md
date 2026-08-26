# Contributing

This is primarily a personal reference implementation — a real, working example of a three-tier AWS architecture cut over to microservices, with Terraform, EKS, ArgoCD GitOps, and a DevSecOps CI/CD pipeline. It's not run as a large open-source project with a formal governance process, but issues and pull requests are welcome.

## Before opening a PR

- Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) first — it documents the current, real state of the system, including past incidents and why things are built the way they are. A change that looks like an obvious improvement may already have a documented reason it isn't done that way.
- For infrastructure changes: run `terraform fmt` and `terraform validate` inside `terraform/` before submitting.
- For application changes: each service has its own test suite (`npm run test`) — see [`README.md`](README.md#local-development) for local dev setup.
- Keep PRs scoped to one change. Unrelated formatting/refactoring noise makes real changes harder to review.

## Reporting bugs

Open a GitHub issue with:
- What you expected vs. what happened
- Terraform/Kubernetes version, AWS region
- Relevant logs (`terraform apply` output, `kubectl describe`/`kubectl logs`, ArgoCD sync errors)

## Security issues

Don't open a public issue for a security vulnerability — see [`SECURITY.md`](SECURITY.md).
