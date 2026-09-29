# testlib — shared test tooling (NOT an action)

`testlib/` is the ONE shared copy of this repo's test helpers. It is **not** a
published composite action: it has no `action.yml`, nothing under it is a `uses:`
target, and it is never pinned or released. It exists only so several actions'
test suites depend on a single, tested implementation instead of hand-copied
duplicates that drift apart.

## What's here

- **`stepvars.py`** — a static guard that asserts every variable a `run:` step
  (or a `${{ env.X }}` expression) dereferences is actually defined before it is
  read: by that step's `env:`, an earlier `$GITHUB_ENV` write, a local
  assignment, a guarding `${NAME:-}` operator, or a documented GitHub default.
  A miss is a `set -u` "unbound variable" that aborts the step on every run, and
  neither `actionlint` nor `shellcheck` models GitHub's env plumbing, so this
  fills that gap. Import it as `from testlib.stepvars import undefined_run_vars`.
- **`tests/`** — `stepvars.py`'s own tests: synthetic composite fixtures plus one
  captured regression fixture (`tests/fixtures/pre_fix_base_branch_action.yml`).
  CI runs them as their own job so the shared guard is proven independently of
  any action that adopts it.

## Adopting the guard

Add the repo root to `sys.path` and import the package module:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(REPO_ROOT))  # repo root, so `testlib` is importable
from testlib.stepvars import undefined_run_vars

findings = undefined_run_vars(Path("my-action/action.yml"))
assert findings == [], findings
```

CI runs each adopting suite from the repo root (`python -m pytest <action>/tests`),
so the repo root is already on `sys.path`; the insert makes the import robust when
a suite is run from elsewhere.
