#!/usr/bin/env bash
# Build the image, push it to ECR and run it on AWS App Runner.
# Needs: aws CLI v2 logged in, docker, and an App Runner ECR access role
# (APPRUNNER_ECR_ROLE_ARN, see README). Usage:
#   HOUSEHOLD_TOKENS="$(openssl rand -hex 24):rossi" BEDROCK_MODEL_ID=... ./infra/deploy-apprunner.sh
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${SERVICE_NAME:-mcp-home}"
: "${HOUSEHOLD_TOKENS:?set HOUSEHOLD_TOKENS=token:household}"
: "${APPRUNNER_ECR_ROLE_ARN:?set APPRUNNER_ECR_ROLE_ARN}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$NAME"

aws ecr describe-repositories --repository-names "$NAME" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$NAME" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
docker build --platform linux/amd64 -t "$REPO:latest" .
docker push "$REPO:latest"

# Optional, for Alexa+ account linking (see README "Connect to Alexa+"): PUBLIC_URL, OAUTH_CLIENT_ID,
# OAUTH_CLIENT_SECRET, OAUTH_REDIRECT_URIS (comma separated), ALEXA_COMPAT=1. A first deploy does not know its URL
# yet: set PUBLIC_URL afterwards in the App Runner console (Configuration > Environment variables), or delete and
# re-run with PUBLIC_URL set. start-deployment below redeploys the image but keeps the service's existing variables.
ENV_VARS=$(printf '{"HOUSEHOLD_TOKENS":"%s","BEDROCK_MODEL_ID":"%s","AWS_REGION":"%s","PUBLIC_URL":"%s","OAUTH_CLIENT_ID":"%s","OAUTH_CLIENT_SECRET":"%s","OAUTH_REDIRECT_URIS":"%s","ALEXA_COMPAT":"%s"}' \
  "$HOUSEHOLD_TOKENS" "${BEDROCK_MODEL_ID:-}" "$REGION" "${PUBLIC_URL:-}" "${OAUTH_CLIENT_ID:-}" \
  "${OAUTH_CLIENT_SECRET:-}" "${OAUTH_REDIRECT_URIS:-}" "${ALEXA_COMPAT:-}")

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
