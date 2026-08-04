# Deploying the kb-connect Slack OAuth Lambda

Follows the same pattern as `infra/provision-console.sh` (Lambda + HTTP API +
CloudFront `/api/*` behavior), scoped to just the one route this needs:
`POST /sources/slackhistory/connect`.

Run these from the repo root in PowerShell. Fill in `$AccountId` /
`$Region` / `$DistributionId` for your setup — `$DistributionId` is
`E2GHQCXZI3EWUV` (the kb-connect CloudFront distribution already created).

```powershell
$AccountId = "661779458398"
$Region = "ap-southeast-2"
$DistributionId = "E2GHQCXZI3EWUV"
$RoleName = "IvvY_KBConnect_OAuth_Role"
$FnName = "kb_connect_oauth"
```

## 1. IAM role for the Lambda

```powershell
aws iam create-role `
  --role-name $RoleName `
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

(Get-Content iam\kb_connect_oauth_policy.json) `
  -replace "REGION", $Region -replace "ACCOUNT_ID", $AccountId |
  Set-Content -Encoding utf8 "$env:TEMP\kb_connect_oauth_policy.json"

aws iam put-role-policy `
  --role-name $RoleName `
  --policy-name KBConnectOAuth `
  --policy-document "file://$env:TEMP/kb_connect_oauth_policy.json"
```

Wait ~10 seconds after creating the role before using it below — IAM roles
aren't always immediately assumable.

## 2. Package and create the Lambda

Pull the Slack client id/secret from `.env` rather than retyping them:

```powershell
$envLines = Get-Content .env
$SlackClientId = ($envLines | Where-Object { $_ -match '^SLACK_CLIENT_ID=' }) -replace 'SLACK_CLIENT_ID=', ''
$SlackClientSecret = ($envLines | Where-Object { $_ -match '^SLACK_CLIENT_SECRET=' }) -replace 'SLACK_CLIENT_SECRET=', ''

Compress-Archive -Path lambda_kb_connect_oauth.py -DestinationPath "$env:TEMP\kb_connect_oauth.zip" -Force

aws lambda create-function `
  --function-name $FnName `
  --runtime python3.12 `
  --handler lambda_kb_connect_oauth.lambda_handler `
  --role "arn:aws:iam::${AccountId}:role/${RoleName}" `
  --zip-file "fileb://$env:TEMP/kb_connect_oauth.zip" `
  --timeout 10 `
  --memory-size 256 `
  --environment "Variables={SLACK_CLIENT_ID=$SlackClientId,SLACK_CLIENT_SECRET=$SlackClientSecret,SECRET_PREFIX=ivvy-bot}" `
  --region $Region
```

## 3. HTTP API + route + Lambda permission

```powershell
$Api = aws apigatewayv2 create-api `
  --name kb-connect-api `
  --protocol-type HTTP `
  --region $Region | ConvertFrom-Json
$ApiId = $Api.ApiId

$LambdaArn = "arn:aws:lambda:${Region}:${AccountId}:function:${FnName}"

$Integration = aws apigatewayv2 create-integration `
  --api-id $ApiId `
  --integration-type AWS_PROXY `
  --integration-uri $LambdaArn `
  --payload-format-version "2.0" `
  --region $Region | ConvertFrom-Json
$IntegrationId = $Integration.IntegrationId

aws apigatewayv2 create-route `
  --api-id $ApiId `
  --route-key "POST /sources/slackhistory/connect" `
  --target "integrations/$IntegrationId" `
  --region $Region

aws apigatewayv2 create-stage `
  --api-id $ApiId `
  --stage-name '$default' `
  --auto-deploy `
  --region $Region

aws lambda add-permission `
  --function-name $FnName `
  --statement-id AllowApiGatewayInvoke `
  --action lambda:InvokeFunction `
  --principal apigateway.amazonaws.com `
  --source-arn "arn:aws:execute-api:${Region}:${AccountId}:${ApiId}/*/*/sources/slackhistory/connect" `
  --region $Region

# The API's invoke domain — needed for the CloudFront origin in step 4.
$ApiEndpoint = (aws apigatewayv2 get-api --api-id $ApiId --region $Region | ConvertFrom-Json).ApiEndpoint
$ApiEndpoint  # e.g. https://abc123xyz.execute-api.ap-southeast-2.amazonaws.com — note the host part
```

## 4. Add the `/api/*` origin + behavior on the existing CloudFront distribution

This is the one step best done in the console, since it means editing the
existing distribution's config in place (origins + cache behaviors) rather
than a single CLI call:

**CloudFront console → distributions → your distribution (`$DistributionId`) → Origins tab → Create origin**
- Origin domain: the host from `$ApiEndpoint` above (no `https://`, no path)
- Protocol: HTTPS only

**→ Behaviors tab → Create behavior**
- Path pattern: `/api/*`
- Origin: the one just created
- Viewer protocol policy: HTTPS only
- Cache policy: **CachingDisabled** (managed policy) — API responses must not be cached
- Origin request policy: **AllViewer** (managed policy) — forwards the POST body through

Save, then wait for the distribution to redeploy (Status: Deploying → Deployed,
a few minutes).

## 5. Point kb-connect at the real API and turn mocking off

Edit `kb-connect/js/config.js`:

```js
window.IVVY_USE_MOCK = false;
window.IVVY_API_BASE = '/api';
```

**Careful:** with `IVVY_USE_MOCK = false`, ALL sources call the real backend —
ServiceNow, Teams, and Atlassian's `connect`/`ingest`/`status` calls will now
404 (their Lambdas don't exist yet), so those three tiles will show "Failed"
once clicked. Only Slack's connect step has a real endpoint after this
deploy. That's expected for now — this was scoped as a Slack-only proof.

Then re-sync and invalidate as before:

```powershell
aws s3 sync "kb-connect" s3://ivvy-kb-connect/ --delete --exclude "screenshots/*" --exclude "HOSTING.md"
aws cloudfront create-invalidation --distribution-id $DistributionId --paths "/*"
```

## 6. Register the redirect URI with the Slack app

In api.slack.com/apps → your app → **OAuth & Permissions → Redirect URLs**,
add:

```
https://d2ro2axkvt2v6v.cloudfront.net/popup.html
```

(or your custom domain's `/popup.html`, if you set one up). Slack rejects the
OAuth exchange if the `redirect_uri` sent doesn't exactly match a registered
one — this has to be done before "Sign in to Slack" can complete.

## 7. Test

Open the CloudFront URL, click "Sign in to Slack" on the Slack history tile.
It should open Slack's real consent screen, redirect back to `popup.html`,
close the popup, and the tile should start reading (mocked ingest progress,
since `ingest`/`status` for Slack still isn't a real endpoint — this step
only proves the *connect* leg).
