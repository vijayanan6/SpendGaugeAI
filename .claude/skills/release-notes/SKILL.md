---
name: release-notes
description: Draft release notes for a SpendGaugeAI publish, covering both the Python package (spendgaugeai on PyPI) and the JS client (spendgaugeai-client on npm). Use before running through docs/PUBLISHING.md, or whenever asked to summarize what's changed since the last release.
disable-model-invocation: true
---

# Release Notes

Drafts release notes from git history for both packages this repo ships. User-invoked only
(`/release-notes`) — this has no side effects, but it's a pre-publish step, not something to run
unprompted.

## Steps

1. **Find the last release point.** Look for the most recent git tag (`git tag --sort=-creatordate
   | head -5`) or, if untagged, the commit that last bumped `version` in `pyproject.toml` and
   `clients/js/package.json` (`git log -p -- pyproject.toml clients/js/package.json` and look for
   the `version` diff lines). If neither exists, use the repo's first commit.

2. **Collect commits since then**, split by what they touch:
   - Python package: commits touching `src/spendgaugeai/`, `pyproject.toml`, `tests/`
   - JS client: commits touching `clients/js/`
   - Shared/docs: commits touching `docs/`, `README.md`, both

   `git log --oneline <last-release>..HEAD -- <paths>` for each group.

3. **Group by kind**, inferring from the commit message prefix this repo actually uses (`feat:`,
   `fix:`, `docs:`, etc. — check `git log --oneline -20` for the real convention in use rather than
   assuming Conventional Commits verbatim). Typical buckets: Features, Fixes, Docs, Internal
   (tests/refactors — usually omit from user-facing notes unless something changed behavior).

4. **Draft two sections** (or one, if only one package changed): "spendgaugeai (Python)" and
   "spendgaugeai-client (JS/TS)", each with the current version bump target and a bullet list.
   Write for someone installing the package, not someone reading the diff — describe user-visible
   effect, not implementation detail.

5. **Present the draft and stop.** This is a draft for Vijay to edit before it's used anywhere
   (a GitHub release, a CHANGELOG, npm/PyPI release notes) — don't write it to any file
   automatically unless asked to.
