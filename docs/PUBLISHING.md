# Publishing SpendGaugeAI

This is the runbook for the one manual step every other doc in this repo defers to Vijay: the
actual PyPI (`spendgaugeai`) and npm (`spendgaugeai-client`) publish. Everything up to this
point — build correctness, install correctness, both SDKs importable by a stranger — is already
verified (see `README.md` and `docs/DESIGN.md` §9). This file only exists so those commands don't
have to be reconstructed from memory when the moment comes.

Not automated by CI on purpose — see `CLAUDE.md` and `docs/DESIGN.md` §9 for why.

## Python — `spendgaugeai` on PyPI

Prerequisite: a PyPI account with a scoped API token (`pypi-...`), and `twine` installed
(`pip install twine`, or `pip install -e ".[dev]"` plus `twine` separately — it's not currently
in the `dev` extra).

```bash
# 1. From the repo root — recompile the CSS fresh so app.css matches the current
#    static/src/input.css before it ships. It's already committed, but this
#    confirms it's not stale (same idempotent step the Dockerfile runs).
./scripts/build-css.sh
git status   # if app.css changed, commit that first — don't publish a stale build

# 2. Bump the version in pyproject.toml ([project] version = "...") — PyPI
#    rejects re-uploading an existing version number, and there's no way to
#    delete-and-retry once a version is live.

# 3. Build sdist + wheel
pip install --upgrade build
python -m build

# 4. Sanity-check the built artifacts before uploading anything
twine check dist/*

# 5. Upload
twine upload dist/*
# prompts for username (use __token__) and password (the pypi-... token)
```

**Verify it actually works** — don't trust the upload succeeding as proof; install it fresh:

```bash
python -m venv /tmp/spendgaugeai-verify
/tmp/spendgaugeai-verify/bin/pip install spendgaugeai   # no --pre, no git+ URL
/tmp/spendgaugeai-verify/bin/spendgaugeai serve --port 8199 &
curl http://localhost:8199/health   # expect {"status":"ok"}
```

Then update `README.md`'s Quickstart: swap the `pip install git+https://...` one-liner for the
real `pip install spendgaugeai` line (the "(Once published: ...)" note already there tells you
exactly what to swap it to — just promote it and drop the git install instructions, or keep both
if you want the git path documented as a fallback).

## JS/TS — `spendgaugeai-client` on npm

Prerequisite: an npm account, logged in locally (`npm login`).

```bash
cd clients/js

# 1. Install, build, and run the test suite — don't publish on a red build
npm install
npm run build
npm run typecheck
npm test

# 2. Bump the version — npm also rejects re-publishing an existing version.
#    Use npm's own bump (updates package.json and creates a git tag):
npm version patch   # or minor / major, per semver

# 3. Sanity-check exactly what will be uploaded before it's irreversible
npm pack --dry-run   # lists every file that would be included
npm publish
```

**Verify it actually works** in a real separate project, not just the local `.tgz`:

```bash
mkdir -p /tmp/sgai-client-verify && cd /tmp/sgai-client-verify
npm init -y
npm install spendgaugeai-client @anthropic-ai/sdk
node -e "const {wrap} = require('spendgaugeai-client'); console.log(typeof wrap)"   # expect "function"
```

Then update `README.md`'s JS/TS section and `clients/js/README.md`: swap the clone-and-build
instructions for the real `npm install spendgaugeai-client` line.

## After both are live

- Update `README.md` (root) — both the Quickstart and the JS/TS integration section have a
  "(Once published: ...)" note showing exactly what each block becomes; promote those, and
  decide whether to keep the git-based install documented as a fallback or drop it.
- Update `CLAUDE.md`'s Current status section — "a real PyPI/npm publish" moves from the open
  items list to done.
- Leave `docs/DESIGN.md` §9 as-is — it already documents *why* publishing is manual, which
  remains true regardless of whether it's happened yet.
