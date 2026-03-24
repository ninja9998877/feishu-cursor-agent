# Security Policy

## Supported Versions

This project is in early stage. Security fixes are provided on the latest `master` branch.

## Reporting a Vulnerability

Please do **not** open public issues for security problems.

Report privately by contacting the repository owner via GitHub account contact channel.
Include:

- what happened
- reproduction steps
- potential impact
- suggested mitigation (optional)

We will acknowledge the report as soon as possible and work on a fix.

## Secrets Handling

Never commit any real secrets:

- `.env`
- Feishu `APP_SECRET`, bot tokens, webhook keys
- personal access tokens
- logs containing sensitive payload

If secrets are exposed:

1. Revoke/rotate them immediately in Feishu/GitHub.
2. Remove leaked values from local environment files.
3. Force regeneration of compromised credentials before redeploy.
