#!/usr/bin/env bash
# Build frontend + backend Docker images and push to ECR.
#
# Usage:
#   ./scripts/build-and-push.sh <AWS_ACCOUNT_ID> <AWS_REGION> <IMAGE_TAG> [REACT_APP_API_URL]
#
# Example:
#   ./scripts/build-and-push.sh 123456789012 us-east-1 v1.0.0 https://api.b17facebook.xyz

set -euo pipefail

AWS_ACCOUNT_ID="${1:?Error: AWS_ACCOUNT_ID required as \$1}"
AWS_REGION="${2:?Error: AWS_REGION required as \$2}"
IMAGE_TAG="${3:-latest}"
REACT_APP_API_URL="${4:-http://localhost:3000}"

REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
FRONTEND_IMAGE="${REGISTRY}/bookstore-frontend:${IMAGE_TAG}"
BACKEND_IMAGE="${REGISTRY}/bookstore-backend:${IMAGE_TAG}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Registry : ${REGISTRY}"
echo "Tag      : ${IMAGE_TAG}"
echo "API URL  : ${REACT_APP_API_URL}"
echo ""

# ── Authenticate to ECR ──────────────────────────────────────────────────────
echo "==> Authenticating to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

# ── Build ────────────────────────────────────────────────────────────────────
echo "==> Building frontend (${FRONTEND_IMAGE})..."
docker build \
  --build-arg REACT_APP_API_URL="${REACT_APP_API_URL}" \
  -t "${FRONTEND_IMAGE}" \
  "${ROOT_DIR}/client"

echo "==> Building backend (${BACKEND_IMAGE})..."
docker build \
  -t "${BACKEND_IMAGE}" \
  "${ROOT_DIR}/backend"

# ── Push ─────────────────────────────────────────────────────────────────────
echo "==> Pushing frontend..."
docker push "${FRONTEND_IMAGE}"

echo "==> Pushing backend..."
docker push "${BACKEND_IMAGE}"

echo ""
echo "Done."
echo "  Frontend : ${FRONTEND_IMAGE}"
echo "  Backend  : ${BACKEND_IMAGE}"
