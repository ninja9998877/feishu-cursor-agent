# Contributing

Thanks for contributing to `feishu-cursor-agent`.

## Development Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Run locally:

```powershell
python main.py
```

## Pull Request Guidelines

- Keep PRs focused and small.
- Explain **why** the change is needed.
- Add/update docs when behavior changes.
- Do not include unrelated refactors.

## Security Requirements

- Never commit `.env`, tokens, secrets, or logs.
- Verify `.gitignore` still excludes sensitive files.
- If a secret is leaked, rotate it immediately and mention mitigation in PR.

## Code Style

- Keep runtime behavior explicit and observable with logs.
- Prefer safe fallbacks over silent failure.
- Preserve compatibility for existing chat command behavior.
