#!/usr/bin/env bash
# Build the image, push it to ECR and run it on AWS App Runner.
# Needs: aws CLI v2 logged in, docker, and an App Runner ECR access role
# (APPRUNNER_ECR_ROLE_ARN, see README). Usage:
#   HOUSEHOLD_TOKENS="$(openssl rand -hex 24):rossi" BEDROCK_MODEL_ID=... ./infra/deploy-apprunner.sh
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${SERVICE_NAME:-casa-mcp}"
: "${HOUSEHOLD_TOKENS:?set HOUSEHOLD_TOKENS=token:household}"
: "${APPRUNNER_ECR_ROLE_ARN:?set APPRUNNER_ECR_ROLE_ARN}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$NAME"

aws ecr describe-repositories --repository-names "$NAME" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$NAME" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
docker build --platform linux/amd64 -t "$REPO:latest" .
docker push "$REPO:latest"

ENV_VARS=$(printf '{"HOUSEHOLD_TOKENS":"%s","BEDROCK_MODEL_ID":"%s","AWS_REGION":"%s"}' \
  "$HOUSEHOLD_TOKENS" "${BEDROCK_MODEL_ID:-}" "$REGION")

if ARN=$(aws apprunner list-services --region "$REGION" \
    --query "ServiceSummaryList[?ServiceName=='$NAME'].ServiceArn | [0]" --output text) && [ "$ARN" != "None" ]; then
  aws apprunner start-deployment --service-arn "$ARN" --region "$REGION" >/dev/null
else
  ARN=$(aws apprunner create-service --region "$REGION" --service-name "$NAME" \
    --source-configuration "{\"AuthenticationConfiguration\":{\"AccessRoleArn\":\"$APPRUNNER_ECR_ROLE_ARN\"},\"AutoDeploymentsEnabled\":false,\"ImageRepository\":{\"ImageIdentifier\":\"$REPO:latest\",\"ImageRepositoryType\":\"ECR\",\"ImageConfiguration\":{\"Port\":\"8000\",\"RuntimeEnvironmentVariables\":$ENV_VARS}}}" \
    --health-check-configuration Protocol=TCP \
    --query Service.ServiceArn --output text)
fi
URL=$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" --query Service.ServiceUrl --output text)
echo "MCP endpoint: https://$URL/mcp"
