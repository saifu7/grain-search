# Contributing to Grain Search

Thanks for helping! Bug reports, ideas, docs fixes and code are all welcome.

## Before you start
- **Bugs:** open an issue with the bug form. Include the app version, Windows version, GPU, and the
  relevant part of `service.log`. **Please remove personal file names and paths** from logs.
- **Bigger changes:** open an issue or a discussion first, so we can agree on the approach before you
  spend time on it.
- **Security problems:** see [SECURITY.md](SECURITY.md). Do not open a public issue.

## Repository layout
| Folder | What |
|---|---|
| `engine/` | Python package `grain_search`: indexing, search, the `grain` CLI and the local service |
| `app/` | Windows app: Tauri 2 (Rust) shell in `src-tauri/`, React UI in `src/` |
| `docs/` | design notes, roadmap, development and release guides |
| `scripts/` | repository tools (version bump, checks) |

Setup: [docs/development.md](docs/development.md).

## Workflow
1. Fork, or create a branch: `feat/short-name`, `fix/short-name`, `docs/…`.
2. Keep each pull request focused on one change. Add or update tests.
3. Run the checks locally (CI runs the same):
   ```
   cd engine && uvx ruff check . && uvx ruff format --check . && uv run pytest
   cd app && npm run typecheck && npm run build
   cd app/src-tauri && cargo fmt --check && cargo clippy -- -D warnings && cargo test
   ```
   Or install the hooks once with `uv tool install pre-commit` and then `pre-commit install`.
4. Open a pull request. Fill in the template. CI must be green before merging.
5. PRs are **squash-merged**, so the PR title becomes the commit message.

## Commit and PR titles: Conventional Commits
`type(scope): summary`, for example `fix(engine): skip images smaller than 32 px` or
`feat(app): remember window position`.
Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`.
A `!` after the type (`feat!:`) marks a breaking change.

Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.

## Code style
- Python: ruff (lint + format), line length 120, type hints on public functions.
- TypeScript: strict mode; keep components small; no layout-shifting UI (see the CLS test).
- Rust: `rustfmt`, `clippy` clean.
- Never commit personal data, indexes, `config.yaml`, `settings.json`, tokens or model weights.
  CI rejects them.

## Licence
By contributing you agree that your contribution is licensed under the [MIT licence](LICENSE).
