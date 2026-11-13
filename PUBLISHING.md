# Publishing — how AiAS Memory gets to OpenClaw users

The channels are a pipeline, not alternatives:

1. **Git is the source of truth.** The plugin + service live in a public
   GitHub repo (INTERCHAINED LLC / aiassistsecure org). Everything else
   publishes *from* the repo.
2. **ClawHub is the official market.** [clawhub.ai](https://clawhub.ai) is
   OpenClaw's public registry for skills and plugins. Users discover and
   install from there:

   ```
   openclaw plugins install clawhub:@interchained/openclaw-aias-memory
   ```

   Publishing is done with the ClawHub CLI, straight from the repo:

   ```
   npm i -g clawhub
   clawhub login
   clawhub package publish aiassistsecure/aias-memory --dry-run   # validate first
   clawhub package publish aiassistsecure/aias-memory
   ```

   npm is the package format underneath ClawHub code plugins; there is no
   separate npm strategy. Do not `npm publish` as a substitute for the
   registry listing.

3. **Visibility extras.** A PR adding the plugin to the community
   `awesome-openclaw` list, and a launch post. Memory backends in this
   ecosystem (Honcho, LanceDB) ship as plugins, not as PRs into OpenClaw
   core, so the registry listing is the upstream presence that matters.

## Publish-time checklist

- [x] Public GitHub repo created under the org; this directory pushed as-is.
- [x] `openclaw/package.json`: add the required ClawHub compatibility
      metadata before publishing — `openclaw.compat.pluginApi` and
      `openclaw.build.openclawVersion`, set to the current OpenClaw
      release's values. The `--dry-run` publish validates them; installs
      fail closed on incompatible hosts.
- [x] Publishing GitHub account passes ClawHub's age gate (accounts must be
      old enough to upload).
- [x] `tests/smoke.py` and `demo.py` green on the exact tree being published.
- [ ] Automated registry scans pass (ClawHub scans every release; releases
      that fail are held from public surfaces).

Status, 2026-11-13: published on the owner's word. GitHub release v1.0.0 is
live, and the ClawHub listing (@interchained/openclaw-aias-memory 1.0.0) is
submitted and pending the registry's security scans, the one box above
still open.
