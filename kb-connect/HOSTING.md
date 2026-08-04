# Hosting kb-connect on S3 + CloudFront

This app is static (HTML/JS/CSS, no build step, no server), so it hosts cleanly
on S3 behind CloudFront. This guide covers only the static hosting layer —
wiring `js/config.js` to a real API (Lambda/API Gateway) is a separate step
that comes after this.

## 1. Create the S3 bucket (private, not "static website hosting" mode)

Use CloudFront's Origin Access Control instead of S3 static-website-hosting +
public bucket — it keeps the bucket private and lets CloudFront be the only
way in.

```
aws s3 mb s3://ivvy-kb-connect --region us-east-1
aws s3api put-public-access-block \
  --bucket ivvy-kb-connect \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

## 2. Upload the app

```
aws s3 sync kb-connect/ s3://ivvy-kb-connect/ --delete \
  --exclude "screenshots/*" --exclude "HOSTING.md"
```

Re-run this `sync` command any time you deploy an update.

## 3. Request/import a TLS certificate (only if using a custom domain)

CloudFront requires the cert to live in **us-east-1**, regardless of where
your bucket is.

```
aws acm request-certificate \
  --domain-name kb.yourdomain.com \
  --validation-method DNS \
  --region us-east-1
```

Add the returned CNAME validation record in your DNS provider, wait for
`Status: ISSUED`.

Skip this step if you're fine using the default `*.cloudfront.net` domain.

## 4. Create the CloudFront distribution

Console path (fastest for a one-off): **CloudFront → Create distribution**

- **Origin domain**: select your S3 bucket (`ivvy-kb-connect.s3.<region>.amazonaws.com`)
- **Origin access**: Origin access control settings (recommended) → create a
  new OAC → CloudFront will give you a bucket policy to paste in (step 5)
- **Viewer protocol policy**: Redirect HTTP to HTTPS
- **Default root object**: `index.html`
- **Alternate domain name (CNAME)**: `kb.yourdomain.com` (skip if no custom domain)
- **Custom SSL certificate**: pick the ACM cert from step 3 (skip if no custom domain)
- **Price class**: your call — "Use only North America and Europe" is
  cheapest if that's your audience

## 5. Attach the bucket policy CloudFront gives you

After creating the distribution, CloudFront shows a bucket policy snippet
for the OAC. Paste it in **S3 → your bucket → Permissions → Bucket policy**.
It looks like:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowCloudFrontServicePrincipal",
    "Effect": "Allow",
    "Principal": { "Service": "cloudfront.amazonaws.com" },
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::ivvy-kb-connect/*",
    "Condition": {
      "StringEquals": { "AWS:SourceArn": "arn:aws:cloudfront::<account-id>:distribution/<distribution-id>" }
    }
  }]
}
```

## 6. Handle the popup route (`popup.html`)

`app.js` opens `popup.html` in a real browser popup for OAuth (see
`js/app.js` → `signIn`). No special CloudFront routing is needed for this —
it's a normal static file at `/popup.html`, served the same way as
`index.html`. Just make sure the sync in step 2 included it (it will, by
default).

**One thing to fix before this works for real OAuth**: `js/sources.js`
computes `REDIRECT_URI` from `location.origin` + `location.pathname`, so once
this is live on `kb.yourdomain.com`, that becomes the redirect URI you must
register in each provider's OAuth app (Atlassian dev console, Slack app
settings, Azure App Registration). Do that *after* this distribution is live,
using the real CloudFront/custom domain — not `localhost`.

## 7. Cache invalidation on deploy

Static assets get cached at CloudFront edge locations, so a new `sync` won't
show up immediately unless you invalidate:

```
aws cloudfront create-invalidation \
  --distribution-id <distribution-id> \
  --paths "/*"
```

Add this as the last step of your deploy script, after the `s3 sync`.

## 8. DNS (only if using a custom domain)

Point `kb.yourdomain.com` at the CloudFront distribution's domain name
(`dxxxxxxxxxxxxx.cloudfront.net`) via a CNAME (or an ALIAS/A-record if your
DNS is Route 53).

## What this does NOT cover

- Wiring `window.IVVY_API_BASE` / `window.IVVY_CLIENT_IDS` in `js/config.js`
  to real Lambda/API Gateway endpoints — that's the next step once this
  hosting is live, and touches `js/backend.js` (currently mocked via
  `IVVY_USE_MOCK`).
- Registering OAuth apps with each provider (Slack, Atlassian, Microsoft,
  ServiceNow) and getting real client IDs.
- Any Lambda/API Gateway/DynamoDB backend infra — none of that is part of
  this static-hosting step.
