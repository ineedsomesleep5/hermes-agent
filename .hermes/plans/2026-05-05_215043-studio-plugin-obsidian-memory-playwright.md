# Plan: Studio as Dashboard Plugin + Preserve Obsidian/Memory + Install Playwright

## Goal

Make Hermes Studio a proper Hermes dashboard plugin while keeping existing Obsidian/wiki and memory workflows working, and install Playwright support safely.

Desired outcome:

- Studio appears in the dashboard as a plugin-backed tab, not as fragile loose dashboard code.
- Obsidian wiki graph/data still resolves from the configured wiki path.
- Hermes memory/session/skills systems keep working unchanged.
- Playwright is installed in a controlled way and does not leave root-owned cache/build debris in the repo.
- Changes are tracked cleanly in Git on `feat/hermes-studio-dashboard-kanban`.
- The running dashboard is refreshed only after validation so Caleb does not lose access.

## Current context / findings

- Active repo: `/opt/hermes`
- Active branch: `feat/hermes-studio-dashboard-kanban`
- Studio was **not deleted**.
- Current untracked Studio source exists under:
  - `web/src/pages/StudioPage.tsx`
  - `web/src/studio/`
  - `web/src/studio-entry.tsx`
  - `tools/studio_*.py`
  - `tools/studio_templates/`
  - `tests/studio/`
- Built dashboard artifacts already contain Studio files:
  - `hermes_cli/web_dist/studio.html`
  - `hermes_cli/web_dist/studio-assets/studio.js`
  - `hermes_cli/web_dist/studio-assets/studio.css`
- Studio disappearing from the dashboard is probably due to stale/running dashboard routing or incomplete route/plugin registration, **not deletion**.
- Root-owned files currently block staging/build/test cleanup:
  - `/opt/hermes/toolsets.py`
  - `/opt/hermes/web/public/fonts`
  - `/opt/hermes/web/public/ds-assets`
  - `/opt/hermes/tools/__pycache__`
  - `/tmp/pytest-of-root`
  - some `.git/objects/*` subdirectories
- Git staging of new Studio files failed with:
  - `error: insufficient permission for adding an object to repository database .git/objects`
- Backups already created:
  - `/opt/data/hermes-studio-git-backups/tracked-20260505-212824.patch`
  - `/opt/data/hermes-studio-git-backups/untracked-20260505-212824.tar.gz`
  - `/opt/data/hermes-studio-git-backups/studio-toolsets-fix.patch`
- Focused tests previously showed:
  - Python syntax: passed
  - Studio tests: `100 passed`, `3 failed`
  - Web build: blocked by root-owned synced assets
- The 3 failures were integration/wiring issues:
  - missing `studio_*` tools in `_HERMES_CORE_TOOLS`
  - missing static `studio` entry in `TOOLSETS`
  - Obsidian graph test leaking real `OBSIDIAN_VAULT_PATH=/root/wiki`

## Architecture decision

Studio should become a dashboard plugin.

Use Hermes’ documented dashboard plugin pattern:

```text
plugins/studio/
├── plugin.yaml                 # optional/agent plugin manifest if Studio exposes tools as plugin too
└── dashboard/
    ├── manifest.json           # dashboard tab metadata
    ├── plugin_api.py           # backend API mounted at /api/plugins/studio/
    └── dist/
        ├── index.js            # bundled Studio frontend plugin entry
        └── style.css           # optional plugin CSS
```

Dashboard plugin tab target:

```json
{
  "name": "studio",
  "label": "Studio",
  "description": "Visual Hermes workspace for widgets, wiki graph, memory surfaces, and agent control panels",
  "icon": "Sparkles",
  "version": "1.0.0",
  "tab": {
    "path": "/studio",
    "position": "after:sessions"
  },
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

Important: keep `/api/studio/*` temporarily as compatibility routes until the plugin API is proven, then migrate to `/api/plugins/studio/*`.

## Proposed approach

1. Fix ownership/root-owned debris first.
2. Restore Studio tab quickly so the dashboard stops feeling broken.
3. Commit the current Studio work cleanly.
4. Convert Studio from built-in route to plugin route.
5. Preserve Obsidian/wiki and memory integrations through compatibility wrappers and explicit tests.
6. Install Playwright using a repo-safe/cache-safe location.
7. Validate with focused tests, web build, and dashboard smoke test.
8. Only then restart/refresh the running dashboard.

## Step-by-step plan

### Phase 0 — Safety checkpoint

- Confirm active branch:
  - `git branch --show-current`
- Confirm existing backup files are present:
  - `/opt/data/hermes-studio-git-backups/tracked-20260505-212824.patch`
  - `/opt/data/hermes-studio-git-backups/untracked-20260505-212824.tar.gz`
  - `/opt/data/hermes-studio-git-backups/studio-toolsets-fix.patch`
- Create one fresh backup before any changes:
  - tracked diff patch
  - untracked tarball
- Do not delete Studio files.
- Do not restart gateway/dashboard until tests/build are good enough.

### Phase 1 — Fix ownership blockers

Required because Git/build/tests currently cannot complete.

Likely command, run as root or with sudo if available:

```bash
chown -R hermes:hermes /opt/hermes/.git/objects
chown hermes:hermes /opt/hermes/toolsets.py
chown -R hermes:hermes /opt/hermes/web/public/fonts /opt/hermes/web/public/ds-assets
chown -R hermes:hermes /opt/hermes/tools/__pycache__ || true
rm -rf /tmp/pytest-of-root
```

If sudo is unavailable, use one of these alternatives:

- restart shell/session as the owning service user with permission
- run a one-time root cleanup through Hostinger console
- rebuild into a clean clone/worktree owned by `hermes`

Validation:

```bash
python3 - <<'PY'
from pathlib import Path
import os
for p in [
  '/opt/hermes/toolsets.py',
  '/opt/hermes/.git/objects',
  '/opt/hermes/web/public/fonts',
  '/opt/hermes/web/public/ds-assets',
]:
    path = Path(p)
    print(p, 'exists=', path.exists(), 'writable=', os.access(path, os.W_OK))
PY
```

### Phase 2 — Restore Studio visibility quickly

Before full plugin migration, verify why Studio disappeared.

Check:

- Does `web/src/App.tsx` still include `/studio` route/nav?
- Does built `hermes_cli/web_dist/assets/index-*.js` include `/studio`?
- Is running dashboard serving `hermes_cli/web_dist` or some older installed asset path?
- Is the browser/mobile caching old JS?
- Does direct route `/studio` load if typed manually?
- Does `/studio.html` load if served directly?

Likely short-term fix:

- rebuild web assets after ownership fix
- ensure `hermes_cli/web_dist` is updated
- restart dashboard/gateway after build
- hard-refresh dashboard on phone/browser

This is temporary. Long-term route should come from `plugins/studio/dashboard/manifest.json`.

### Phase 3 — Commit current Studio source safely

Stage only intentional source/test files.

Include:

```text
hermes_cli/web_server.py
web/src/App.tsx
web/src/pages/StudioPage.tsx
web/src/studio-entry.tsx
web/src/studio/
tools/background_jobs.py
tools/delegate_background.py
tools/studio_browser_server.js
tools/studio_chat.py
tools/studio_presets.py
tools/studio_templates/
tools/studio_widget_tools.py
tools/studio_widgets.py
tests/studio/
tests/tools/test_delegate_background.py
toolsets.py
```

Do not stage:

```text
tinker-atropos
ui-tui/package-lock.json
.playwright/
hermes_cli/studio_dist/
hermes_cli/web_dist.tmp/
web/public/fonts/
web/public/ds-assets/
```

Apply toolset fix:

```bash
git apply /opt/data/hermes-studio-git-backups/studio-toolsets-fix.patch
```

Then stage and commit:

```bash
git add hermes_cli/web_server.py web/src/App.tsx web/src/pages/StudioPage.tsx web/src/studio-entry.tsx web/src/studio \
  tools/background_jobs.py tools/delegate_background.py tools/studio_browser_server.js tools/studio_chat.py \
  tools/studio_presets.py tools/studio_templates tools/studio_widget_tools.py tools/studio_widgets.py \
  tests/studio tests/tools/test_delegate_background.py toolsets.py

git diff --cached --check
git commit -m "feat: add Hermes Studio dashboard workspace"
```

### Phase 4 — Convert Studio to dashboard plugin

Create:

```text
plugins/studio/dashboard/manifest.json
plugins/studio/dashboard/plugin_api.py
plugins/studio/dashboard/dist/index.js
plugins/studio/dashboard/dist/style.css
```

Move or wrap backend routes:

- Existing temporary/internal routes:
  - `/api/studio/spaces`
  - `/api/studio/spaces/{space_id}`
  - `/api/studio/spaces/{space_id}/widgets/{widget_id}`
  - `/api/studio/presets`
  - `/api/studio/presets/install`
  - `/api/studio/obsidian-graph`
  - `/api/studio/browser/*`
  - `/api/studio/jobs/*`
- Plugin target routes:
  - `/api/plugins/studio/spaces`
  - `/api/plugins/studio/spaces/{space_id}`
  - `/api/plugins/studio/spaces/{space_id}/widgets/{widget_id}`
  - `/api/plugins/studio/presets`
  - `/api/plugins/studio/presets/install`
  - `/api/plugins/studio/obsidian-graph`
  - `/api/plugins/studio/browser/*`
  - `/api/plugins/studio/jobs/*`

Recommended migration pattern:

- Put implementation functions in shared modules:
  - `tools/studio_widgets.py`
  - `tools/studio_presets.py`
  - `tools/studio_chat.py`
  - `tools/background_jobs.py`
- Plugin API imports those shared modules.
- Leave `/api/studio/*` as compatibility shims for one release.
- Studio frontend should prefer plugin API base:
  - `/api/plugins/studio`
- Fallback to `/api/studio` only if plugin route is unavailable.

Frontend plugin strategy:

- Option A, fastest: bundle current `web/src/studio/*` as plugin `dist/index.js` using Vite library mode.
- Option B, cleaner: make `web/src/studio` package-like, then import it from a plugin entry.
- Plugin should use `window.__HERMES_PLUGIN_SDK__` and `window.__HERMES_PLUGINS__.register("studio", StudioPage)`.

After plugin works:

- remove built-in `/studio` nav item from `web/src/App.tsx`
- let plugin manifest add the sidebar item
- enable plugin in config if needed:
  - `plugins.enabled: [studio, kanban, ...]`

### Phase 5 — Preserve Obsidian/wiki and memory

Do not break these existing paths/configs:

- Wiki path currently standardized to `/root/wiki` via config/env.
- `OBSIDIAN_VAULT_PATH` may be set and should keep priority in production.
- Config also has wiki path under `skills.config.wiki.path`.
- Memory/session stores should stay under Hermes home/profile paths.

Implementation rules:

- Use `get_hermes_home()` for Hermes-owned runtime data.
- Do not hardcode `/root/wiki` inside Studio code.
- Resolve wiki path in order:
  1. explicit request/config override, if provided
  2. `OBSIDIAN_VAULT_PATH`
  3. `skills.config.wiki.path`
  4. profile default: `get_hermes_home() / "profiles" / "supervisor" / "home" / "wiki"`
- Tests must isolate env vars:
  - unset or monkeypatch `OBSIDIAN_VAULT_PATH`
  - set `HERMES_HOME` to temp dir
- Studio should read wiki graph as read-only unless the user explicitly asks for edits.
- Memory surfaces should be read-only dashboards at first.
- Any memory write/edit actions must go through existing Hermes memory APIs/tools, not direct DB/file hacks.

Test targets:

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/opt/data/tmp python -m pytest -n0 --basetemp=/opt/data/tmp/pytest-hermes \
  tests/studio/test_web_server_widgets.py::test_obsidian_graph_indexes_vault_wikilinks \
  tests/studio/test_web_server_widgets.py::test_log_endpoint_accepts_widget_friendly_aliases \
  -q
```

Expected result:

- Obsidian graph test returns exactly temp test nodes.
- Production still resolves configured `/root/wiki`.

### Phase 6 — Install Playwright safely

Goal: Playwright available for Studio/browser widgets/tests without polluting repo or creating root-owned artifacts.

Rules:

- Do not install browsers into `/opt/hermes/.playwright` if avoidable.
- Use a data/cache location owned by `hermes`:
  - `/opt/data/playwright-browsers`
- Add local ignore for accidental repo cache:
  - `.playwright/`
- If Playwright is a runtime dependency for `tools/studio_browser_server.js`, add it to the correct package manifest, likely `web/package.json` or a dedicated `tools/package.json` if one exists.
- Prefer npm install as `hermes` user, not root.

Likely commands after ownership cleanup:

```bash
cd /opt/hermes/web
npm install -D playwright
PLAYWRIGHT_BROWSERS_PATH=/opt/data/playwright-browsers npx playwright install chromium
```

If backend Node script runs outside `web/`, choose one of:

- move `studio_browser_server.js` under a Node package with its own `package.json`
- or run it with `NODE_PATH=/opt/hermes/web/node_modules`
- or add Playwright to root/package dependency if root package exists

Validation:

```bash
cd /opt/hermes/web
PLAYWRIGHT_BROWSERS_PATH=/opt/data/playwright-browsers node -e "require('playwright'); console.log('playwright ok')"
```

And if `studio_browser_server.js` has a health endpoint:

```bash
PLAYWRIGHT_BROWSERS_PATH=/opt/data/playwright-browsers node /opt/hermes/tools/studio_browser_server.js
```

Run this as a controlled background process only during test, then stop it.

### Phase 7 — Validation before restart

Python syntax:

```bash
cd /opt/hermes
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python - <<'PY'
from pathlib import Path
files = [
 'tools/background_jobs.py',
 'tools/delegate_background.py',
 'tools/studio_chat.py',
 'tools/studio_presets.py',
 'tools/studio_widget_tools.py',
 'tools/studio_widgets.py',
 'hermes_cli/web_server.py',
 'toolsets.py',
]
for f in files:
    compile(Path(f).read_text(), f, 'exec')
    print('ok', f)
PY
```

Focused tests:

```bash
cd /opt/hermes
mkdir -p /opt/data/tmp/pytest-hermes
TMPDIR=/opt/data/tmp PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -n0 \
  --basetemp=/opt/data/tmp/pytest-hermes \
  tests/studio tests/tools/test_delegate_background.py -q
```

Web build:

```bash
cd /opt/hermes/web
npm run build
```

Dashboard/plugin smoke checks:

- Confirm `plugins/studio/dashboard/manifest.json` is discovered.
- Confirm dashboard nav shows Studio from plugin manifest.
- Confirm `/studio` route loads.
- Confirm `/api/plugins/studio/spaces` returns JSON under dashboard auth/session.
- Confirm `/api/plugins/studio/obsidian-graph` returns wiki graph.
- Confirm memory/session pages still load.
- Confirm Kanban plugin still loads.

### Phase 8 — Controlled dashboard restart

Only after validation:

- Restart gateway/dashboard using the existing Hermes service command.
- Verify running dashboard serves updated `web_dist`.
- Hard-refresh Caleb’s phone/browser.
- If Studio still does not appear:
  - check plugin enabled state
  - check dashboard plugin manifest load errors
  - check browser console
  - temporarily restore built-in route while plugin issue is fixed

Rollback path:

- Use Git branch/commit rollback if committed.
- Use backup tar/patch if commit could not happen.
- Re-enable old built-in `/studio` route if plugin loading fails.

## Files likely to change

Core Studio/plugin files:

```text
plugins/studio/dashboard/manifest.json
plugins/studio/dashboard/plugin_api.py
plugins/studio/dashboard/dist/index.js
plugins/studio/dashboard/dist/style.css
web/src/studio-entry.tsx
web/src/studio/
web/src/pages/StudioPage.tsx
web/src/App.tsx
tools/studio_widgets.py
tools/studio_widget_tools.py
tools/studio_presets.py
tools/studio_chat.py
tools/studio_browser_server.js
tools/background_jobs.py
tools/delegate_background.py
tools/studio_templates/
toolsets.py
```

Tests:

```text
tests/studio/
tests/tools/test_delegate_background.py
```

Build/dependency files, if Playwright is added:

```text
web/package.json
web/package-lock.json
```

Ignore/config files:

```text
.gitignore                  # if writable; otherwise .git/info/exclude for local-only ignores
.git/info/exclude           # local build/cache ignore fallback
/opt/data/config.yaml       # only if plugin enablement must be updated
```

## Tests / validation checklist

Must pass before calling this done:

- Python compile check for Studio/backend files.
- `tests/studio` focused pytest passes.
- `tests/tools/test_delegate_background.py` passes.
- Web build passes.
- Dashboard loads.
- Studio appears as plugin tab.
- Studio can list/create/read widgets.
- Studio can install presets.
- Studio Obsidian graph reads configured wiki.
- Existing memory/session pages still work.
- Existing Kanban plugin still works.
- Playwright import works.
- Playwright Chromium installed under `/opt/data/playwright-browsers`, not repo.

## Risks and mitigations

### Risk: Studio disappears again

Cause:

- plugin disabled
- stale frontend build
- browser cache
- plugin manifest failure

Mitigation:

- keep compatibility built-in route until plugin is verified
- do not remove `StudioPage` until plugin route loads
- smoke test direct `/studio`
- inspect browser console/plugin discovery endpoint

### Risk: Obsidian wiki breaks

Cause:

- route moved to plugin and path resolution changes
- tests/production env var precedence confused

Mitigation:

- centralize wiki path resolver
- preserve `OBSIDIAN_VAULT_PATH` and config lookup
- add tests that isolate env and config
- read-only graph first

### Risk: memory breaks

Cause:

- Studio directly reads/writes memory files/DB incorrectly

Mitigation:

- Studio memory surfaces read-only at first
- writes go through existing Hermes tools/APIs
- add smoke test for sessions/memory pages after migration

### Risk: Playwright creates root-owned or huge repo files

Cause:

- installing as root
- default browser cache path under repo

Mitigation:

- install as `hermes`
- set `PLAYWRIGHT_BROWSERS_PATH=/opt/data/playwright-browsers`
- ignore `.playwright/`
- verify ownership after install

### Risk: Git remains blocked

Cause:

- root-owned `.git/objects` subdirectories

Mitigation:

- fix ownership before staging
- if not possible, create clean clone/worktree owned by `hermes`
- keep backup tar/patch current

### Risk: Dashboard plugin routes unauthenticated if exposed publicly

Cause:

- existing plugin route behavior may bypass auth depending on server settings

Mitigation:

- verify auth middleware behavior for `/api/plugins/studio/*`
- require dashboard session token for write routes if plugin routes bypass auth
- keep dashboard bound/private behind existing auth/proxy

## Open questions

1. Should Studio be enabled for all profiles or only Caleb’s main/supervisor profile?
2. Should Studio be bundled under repo `plugins/studio` or installed as user plugin under `/opt/data/plugins/studio`?
   - Recommendation: bundled during development under `/opt/hermes/plugins/studio`, then decide later.
3. Should Studio memory widgets be read-only initially?
   - Recommendation: yes, read-only first.
4. Should Playwright be a required dependency or optional feature?
   - Recommendation: optional; Studio should degrade gracefully if Playwright is missing.
5. Should `/api/studio/*` remain permanently or be removed after plugin migration?
   - Recommendation: keep compatibility temporarily, remove later after dashboard plugin route is stable.

## Expected final outcome

When complete:

- Caleb opens the dashboard and sees Studio as a real plugin tab.
- Kanban remains available as its own plugin tab.
- Studio can show/manage widgets and presets.
- Studio can display the Obsidian wiki graph without breaking `/root/wiki` or configured wiki paths.
- Hermes memory/session/skills pages still work.
- Playwright-backed Studio browser features work when enabled.
- The repo has clean commits and no untracked root-owned Studio source mess.
- Build/test artifacts live under `/opt/data`, not inside source control.
