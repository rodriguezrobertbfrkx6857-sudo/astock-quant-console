# Public release verification

Verified on Windows with Python 3.13 in the available development environment:

- Empty control and accounting schema initialization, including repeat initialization/checksum verification: passed.
- Default source-only pytest selection: 165 passed, 2 skipped (full workbench configuration required).
- Frontend: clean `npm ci --ignore-scripts --no-audit --no-fund`, then `npm run build`: passed. Vite reports a non-fatal warning about bundle size.
- GET checks for `/`, `/api/v2/overview`, `/api/v2/system`, `/api/v2/research/overview`, `/api/v2/paper/portfolio`, `/api/v2/watch`: HTTP 200 after initialization/build.
- Source release scanner: no findings.
- Exact comparison against credentials extracted from the private local configuration/credential inputs: no matches in the public source.

The public profile remains unready without data. Schema initialization did not import private market data, OOS history, qualified accounts, or results. Generated databases, dependencies, caches and frontend build output were removed after verification and are excluded from the ZIP.

FastAPI/Starlette emitted deprecation warnings for the legacy startup hook and the installed test-client integration; these did not fail tests. The included GitHub Actions workflow targets Linux/Python 3.12 and Node 20; that hosted workflow has not been executed during local preparation. Dependency version ranges are not a complete reproducible Python lockfile. Historical contract/integration suites requiring prepared data were not claimed as passing.

The focused scanner is a practical release check, not proof that every possible secret format can be detected. Re-run it and review staged files before later public commits.
