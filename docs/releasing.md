# Releasing

Releases are built by GitHub Actions from a version tag and appear on the
[Releases page](../../../releases) with the installer and checksums attached.

## Versions
- One version for everything, in [SemVer](https://semver.org): `engine/pyproject.toml`,
  `app/package.json`, `app/src-tauri/Cargo.toml`, `app/src-tauri/tauri.conf.json`.
- `uv run --no-project python scripts/bump_version.py 0.2.0` updates all four (and the lock files' own entries).
  `uv run --no-project python scripts/check_repo.py` (run in CI) fails if they disagree.
- Pre-releases use `-alpha.N`, `-beta.N` or `-rc.N` (`v0.2.0-beta.1`). They are published as GitHub
  *pre-releases*: not shown as "Latest" and not offered to stable auto-update users.

## Checklist
1. Everything for the milestone is merged and CI on `main` is green.
2. Create a branch `release/vX.Y.Z` and run `uv run --no-project python scripts/bump_version.py X.Y.Z`.
3. In `CHANGELOG.md`, rename **Unreleased** to `[X.Y.Z] - YYYY-MM-DD` and add an empty Unreleased
   section above it. Open a PR titled `chore(release): vX.Y.Z` and squash-merge it.
4. Tag the merge commit and push the tag:
   ```
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
5. The **Release** workflow builds everything (~20 min) and creates a **draft** release with:
   `GrainSearch-X.Y.Z-x64-setup.exe`, its `.sig`, `latest.json`, the engine wheel and `SHA256SUMS.txt`.
6. Smoke test the draft's installer on a clean Windows PC or VM:
   install → first run → search a document and a photo → update from the previous version → uninstall.
7. Publish the draft. Close the milestone.
8. Found a problem after publishing? **Never move or delete a published tag.** Fix it and release
   `X.Y.Z+1`.

## Secrets (repository → Settings → Secrets and variables → Actions)
| Secret | Used for | Created by |
|---|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | signing update files so the app can verify them | `npx tauri signer generate -w grain-search.key` (run once, keep the key file safe, **never commit it**) |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | password for that key | chosen when generating |
| `AZURE_*` (later) | Windows code signing (Azure Trusted Signing) | maintainer's Azure account |

The matching **public** key goes into `app/src-tauri/tauri.conf.json` → `plugins.updater.pubkey`
when the updater is switched on (roadmap Phase 5). Until the signing secret exists, the release
workflow still builds the installer and simply skips updater signatures.
