# `.github/` workflow templates

The CI workflow lives here as a template instead of under
`.github/workflows/` because the OAuth token used to push this branch
does not carry the `workflow` scope and GitHub refuses to create or
update workflow files without it.

## To activate CI

Once you (or any operator with a token that has the `workflow` scope)
runs:

```bash
mkdir -p .github/workflows
cp docs/templates/ci.yml.template .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: activate GitHub Actions workflow"
git push
```

Or, refresh the existing `gh` CLI auth to add the `workflow` scope
**interactively** and re-run `git push`:

```bash
gh auth refresh -s workflow
# follow the browser flow, then:
mkdir -p .github/workflows
cp docs/templates/ci.yml.template .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: activate GitHub Actions workflow"
git push
```

## What the workflow does

See `docs/CI.md` for the full description of the three jobs (`backend`,
`frontend`, `api-smoke`), the synthesized NewsImpact governance fixture,
the wheel canary, the security-header probes, and the paste-news demo
that runs end-to-end against `docs/examples/news_fed.txt`.

## Why not auto-install via `gh secret set` or similar

CI activation is a privileged operation (it grants automation the ability
to run code on push). We deliberately make it a manual step so reviewers
see the activation in the PR and can audit the workflow contents before
it ever runs. This also matches the project's broader posture of never
performing privileged actions without explicit operator confirmation.
