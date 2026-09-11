# CODEBASE AUDIT REPORT

Baseline captured on 2026-09-11 before refactoring, from clean `main` commit
`9a45c5107fcc120dbf7ab99ac952187da889d087`.

## Scope and method

The audit enumerated every Git-tracked file and applied the appropriate review
to its type: Python AST/compile and lint checks, Django checks and test
discovery, JavaScript parsing, JSON/YAML/TOML/XML parsing, shell parsing from
the LF-normalized Git index, raster-image verification, duplicate hashes,
reference searches, line/function/class metrics, complexity checks, template
inheritance/include inspection, and security/dependency scans. Binary,
generated migration, vendored, test, documentation, and application files are
reported separately rather than being treated as equivalent source.

## Project Statistics

| Measure | Baseline |
| --- | ---: |
| Git-tracked files | 1,060 |
| Text lines (including tests, migrations, docs, and lock files) | 243,931 |
| Python files | 572 |
| Python lines | 146,731 |
| Python application files (excluding tests and migrations) | 255 |
| Python application lines | 85,901 |
| Templates | 240 classified template files (236 `.html`) |
| Template lines | 81,973 |
| Test files | 162 |
| Discovered Django tests | 2,343 |
| Test assertions (static count) | 6,324 |
| Migration files | 152 |
| Static-source files | 63 |
| Flutter client files | 53 total; 37 source/config files in the inventory classification |

Baseline validation:

- Django test suite: **2,343 passed, 1 skipped** in 417.833 seconds.
- Ruff configured gate: passed.
- Python compileall: passed.
- Django system check: passed with no silenced issues.
- Migration drift: none.
- JavaScript/CJS parsing: all 32 files passed `node --check`.
- JSON/YAML/TOML/XML parsing: passed.
- Raster verification: all 50 PNG/ICO files passed.
- High-confidence Bandit gate: passed.
- Locked dependency audit: no vulnerability finding; the auditor recommends
  adding hashes to the fully pinned lock file.
- Direct requirements/lock consistency: 46 direct pins covered by 97 locked
  packages.

## Repository map

| Area | Responsibility | Initial classification |
| --- | --- | --- |
| `config/` | Django settings, URLs, ASGI, WSGI, Celery | Active, critical, needs decomposition |
| `core/` | request identity, health, metrics, observability, limits cache | Active, critical |
| `reports/` | primary business domain, APIs, services, models, views, tasks | Active, critical, several oversized modules |
| `reports/model_parts/` | domain model implementation behind `reports.models` compatibility facade | Active; import-boundary debt |
| `reports/views/` | domain-specific Django views behind `reports.views` facade | Active; wildcard-import debt |
| `maintenance/` | school-year reset preview/execution | Active, critical operations |
| `operations/` | protected host/project operations API | Active, critical operations |
| `operations_mobile/` | Flutter Android operations client | Active, separately built/tested |
| `static/` | first-party CSS/JS/PWA plus vendored assets | Mixed active and vendored/generated assets |
| `deploy/` and `.github/` | production composition, backup/restore, CI/CD | Active, critical |
| `scripts/` | repository and operator checks | Active developer/operations tooling |
| `docs/` | architecture, operations, security, user guidance | Active documentation |
| `reports/migrations/`, `maintenance/migrations/`, `operations/migrations/` | immutable schema history | Generated but critical; do not hand-edit for cleanup |
| `static/vendor/`, `static/js/vendor/` | third-party distributions | Vendored/generated; excluded from style rewrites |

## FILE SIZE AUDIT

Largest application/source files at baseline (physical lines from the AST
scan; template/CSS numbers use text lines):

| File | Lines | Functions | Classes | Approx. branch/decision count | Finding |
| --- | ---: | ---: | ---: | ---: | --- |
| `reports/forms.py` | 3,494 | 99 | 70 | 575 | God module; split only with form-contract tests |
| `reports/templates/reports/my_subscription.html` | 3,086 | n/a | n/a | n/a | CSS, markup, and scripts mixed |
| `static/css/app.css` | 2,922 | n/a | n/a | n/a | Legacy cascade and override concentration |
| `reports/views/reports.py` | 2,817 | 43 | 0 | 436 | Multiple report/archive/share responsibilities |
| `reports/templates/reports/admin_dashboard.html` | 2,775 | n/a | n/a | n/a | Dashboard CSS/markup/script co-location |
| `reports/mansour_assistant.py` | 2,686 | 60 | 2 | 394 | Retrieval, intent, fallback, and provider client mixed |
| `reports/views/schools.py` | 2,528 | 54 | 6 | 451 | School admin, dashboard, departments, audit mixed |
| `reports/services_export.py` | 2,440 | 39 | 0 | 474 | XLSX, index, archive, and ZIP builders mixed |
| `reports/views/billing_platform.py` | 2,397 | 39 | 1 | 402 | Billing administration plus knowledge/settings |
| `reports/tasks.py` | 2,166 | 40 | 0 | 272 | Unrelated task families share one module |
| `static/css/design-system.css` | 2,494 | n/a | n/a | n/a | Large but cohesive generated design foundation |
| `config/settings.py` | 1,881 | 11 | 0 | 345 | Environment parsing and service policies mixed |

The longest production functions are `build_school_export_zip_file` (587
lines), `build_year_archive_index_bytes` (541),
`build_school_export_workbook` (419), `_offline_customer_reply` (403),
`achievement_file_detail` (399), `_generate_report_pdf_fallback` (383), and
`NotificationCreateForm.save` (379). Ruff's complexity scan at threshold 20
identified 36 functions.

## Dead Code Candidates

Confirmed before removal:

- `reports.views.teachers._legacy_bulk_import_teachers`: 346-line predecessor
  with no route, import, call, or test reference. The route points to the new
  preview-first `teacher_onboarding` workflow.
- `_dark_extracted.css.part`: unreferenced intermediate dark-mode extraction;
  its relevant rules are already in tracked CSS.
- `.codex-repair/post_reboot_verify.ps1`: unrelated Windows device-repair
  artifact with no project reference.
- Nine private helpers have definition-only references and are superseded by
  current implementations or call paths: two department detectors, two form
  membership predicates, one Mansour rewrite wrapper, one model filename
  helper, one department ticket-stat helper, and two display-label helpers.

Potential dead code requiring runtime/product confirmation:

- Four email fragment templates have no literal template reference. They may
  still be selected by database/provider template names, so they must not be
  deleted from static evidence alone.
- Migration compatibility branches and legacy field aliases are deliberately
  retained for old rows and clients.

## Duplicate Code

- `_ensure_achievement_sections` is identical in `_helpers.py` and
  `achievements.py`; it belongs in the achievement service.
- `_school_or_redirect` is identical across assignments, circular drafts,
  documents, meetings, and plans.
- `_superuser_required` is identical in customer-care and platform-email
  views.
- executive-director group validation is repeated in three group view modules.
- customer-care and platform-email views duplicate an IP extractor that trusts
  raw `X-Forwarded-For`, while `core.client_ip` already implements trusted-proxy
  resolution.
- Three logo files are byte-identical but intentionally live in web/PWA and
  Flutter build locations; deduplicating them would break independent build
  inputs for negligible storage benefit.

## Architecture Issues

- Four import strongly-connected components exist. The largest spans tasks,
  exports, PDFs, notifications, billing views, and the shared view helper.
- `reports.views.__init__` imports 42 view modules into a single compatibility
  namespace; `reports.model_parts.__init__` does the same for 23 model parts.
- `reports/views/_helpers.py` and `reports/model_parts/base.py` are wildcard
  dependency bags. This hides each module's real dependencies and prevents a
  reliable global unused-import rule.
- Billing is split physically but still connected through transitive wildcard
  imports, so the boundary is not yet an actual dependency boundary.
- Template-local CSS/JS is widespread: 168 templates inherit a base and 87 use
  includes, but many of the largest screens still combine all three layers.

## Maintainability Issues

- The configured Ruff gate intentionally omits full `F` rules. An exploratory
  `F401/F841` pass reports 381 findings; most are false positives caused by
  wildcard re-export facades, but a smaller set are ordinary unused imports or
  locals and should be removed.
- 1,090 `!important` tokens remain across CSS/templates. The existing UI audit
  identifies this as legacy cascade debt; mechanical removal would regress
  dark mode and responsive behavior.
- The repository has no single generated, file-by-file inventory for handover.
- Root `README.md` is useful but does not yet document domain boundaries,
  external-service failure modes, all build targets, or the refactor/debt
  process.
- Python type hints are present in newer services but inconsistent in older
  view/form modules. No mypy/pyright gate is configured.

## Security Findings

- **P1 external remediation:** `.env` is absent from the current tracked tree,
  but remains in 19 Git-history commits. Values ever stored there must be
  considered exposed and rotated; code cleanup cannot revoke them.
- **P1 audit provenance:** two back-office view modules trust raw
  `X-Forwarded-For` for audit IPs. A caller can spoof that value unless it is
  accepted only from a configured trusted proxy.
- **P2 mobile configuration:** Firebase's Android client API key is tracked in
  `google-services.json`. This is normal client configuration rather than a
  server secret, but its Google/Firebase API and application restrictions must
  be verified outside the repository.
- **P2 deployment privilege:** the managed deployment-key workflow has
  historically granted broad passwordless root privilege; reduce it to an
  allow-listed deployment command in a dedicated infrastructure change.
- No current tracked private key, database password, Django secret, or server
  API credential was found by the repository guard/high-confidence scans.

## Performance Findings

- Existing code already uses `select_related`, `prefetch_related`, annotations,
  pagination, asynchronous exports, and isolated rate-limit storage in many hot
  paths.
- Large dashboard/report views still combine query construction, aggregation,
  presentation shaping, and rendering, making N+1 regressions hard to detect.
- Export builders materialize substantial work in single functions. They are
  protected by async offload but need explicit memory/query budgets before a
  safe split.
- Several context processors perform defensive dynamic model inspection and
  broad fallback logic on every request. Query-count characterization should
  precede deeper simplification.

## Test Coverage Risks

- Overall regression coverage is unusually broad (2,343 tests), including
  authentication, permissions, payments, school isolation, archive/export,
  notifications, PWA, design-system, and deployment configuration.
- One PDF test is skipped on this Windows host because native WeasyPrint
  libraries are unavailable; Docker/CI remains the authoritative PDF build
  environment.
- No enforced typecheck exists.
- Browser-computed layout, dark-mode contrast, and viewport behavior require
  the repository's browser audit or an enabled browser session; template-string
  assertions alone are not full visual proof.
- Flutter has a separate analyzer/test lifecycle and is not covered by Django
  CI assertions unless its commands are run explicitly.

## Priority

### P0 Critical

No confirmed current-code P0 defect was found in the baseline.

### P1 High

- Remove confirmed dead production code and unrelated root artifacts.
- Stop spoofable audit-IP extraction by reusing trusted-proxy resolution.
- Preserve/verify authentication, payment, permission, tenant-isolation, and
  webhook behavior during all refactors.
- Rotate credentials present in historical `.env` commits if rotation is not
  already independently proven.

### P2 Medium

- Centralize the four confirmed duplicate helper families.
- Enable an enforceable unused-import/local gate without breaking intentional
  compatibility facades.
- Split the largest business modules in small domain slices, beginning with
  export builders and forms only after characterization tests.
- Extract page-local CSS/JS from the largest interactive templates by journey,
  with browser regression evidence.
- Break import cycles and replace wildcard dependency bags with explicit
  imports incrementally.

### P3 Low

- Add type checking one typed boundary at a time.
- Reduce legacy CSS specificity and `!important` only alongside visual tests.
- Verify optional Firebase key restrictions and document ownership/rotation.

## Safe execution order

1. Record the inventory and baseline (this report).
2. Remove confirmed dead files/functions and ordinary unused imports.
3. Add trusted-IP characterization tests, then centralize audit IP handling.
4. Centralize exact duplicate helpers with compatibility-preserving imports.
5. Harden linting and documentation.
6. Run targeted tests after every slice and the full Django/Flutter/build gates
   at the end.
7. Keep the remaining large-file/import/template work in `TECHNICAL_DEBT.md`
   unless its complete behavior can be protected and verified in this phase.
