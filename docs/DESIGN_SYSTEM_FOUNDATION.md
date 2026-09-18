# Tawtheeq Design System Foundation

Date: 2026-09-18
Scope: shared web UI foundation only; no business logic, database, route, permission, PWA architecture, or Flutter changes.

## Canonical source

`static/css/tokens.css` is the single runtime source of truth for shared design tokens. New CSS must use the `--twq-*` namespace directly.

The previous unprefixed names (`--primary`, `--surface`, `--space-*`, and similar) and identity names (`--id-*`) remain as compatibility aliases in the same file. They are not a second system and must not receive new literal values outside a documented compatibility exception.

## Token sources before this phase

The main overlapping sources were:

- `static/css/tokens.css`: global semantic tokens and legacy aliases.
- `static/css/app.css`: two generations of global brand, surface, radius, shadow, spacing, motion, and z-index values.
- `static/css/dark-mode.css`: global and module-specific dark values.
- `reports/templates/reports/_identity.html`: brand scales, semantic identity values, workflow tones, radius, elevation, and motion.
- `static/css/royal-theme.css`: royal/brand compatibility variables.
- `static/css/utilities.css`: a legacy utility spacing scale.
- standalone and module templates: local variables scoped to their page.

Page-scoped variables remain temporarily because removing them without migrating their consumers would be a visual regression risk.

## Token source after this phase

- Canonical names and light/dark values: `static/css/tokens.css` under `--twq-*`.
- Compatibility names: aliases in `static/css/tokens.css`; remove only when repository usage reaches zero.
- Page/module variables: temporary consumers. New module CSS must reference canonical tokens instead of defining another palette or scale.
- Identity include: retained for its existing helpers and fallback declarations; canonical tokens load later and govern the final computed semantic values.

## Runtime cascade governance

The current safe migration order in `reports/templates/base.html` is:

1. Legacy reset/base (`app.css`).
2. Legacy shell and optional global modules (`app-shell.css`, assistant, mobile, royal).
3. Page/module CSS emitted through the existing `head` block.
4. Existing shared components and utilities (`app-components.css`, `utilities.css`, `extracted.css`).
5. Legacy dark-mode compatibility (`dark-mode.css`).
6. Canonical tokens and compatibility aliases (`tokens.css`).
7. Shared application foundation and `.twq-*` components (`design-system.css`).

Loading canonical tokens after legacy variable declarations is a deliberate migration exception: it guarantees that older `:root` and dark-mode variables cannot retake semantic authority. Do not move `tokens.css` earlier until legacy aliases and theme declarations have been measured and migrated.

The target authoring model for all new CSS remains:

1. canonical tokens;
2. global layout;
3. shared components;
4. utilities;
5. module-specific CSS;
6. narrow, documented compatibility overrides.

Do not solve a cascade conflict by adding `!important`. Reduce scope/specificity or move the rule into the correct layer.

## Canonical token groups

- Brand: `--twq-primary*`, `--twq-accent*`, stable green/gold scales.
- Semantic: success, warning, danger, info, neutral, and draft with soft surfaces.
- Surfaces/text/borders: background, surface levels, text levels, border/divider/focus.
- Typography: Cairo family, sizes, line heights, and weights.
- Spacing: 4px scale from `--twq-space-0` through `--twq-space-16`.
- Shape/elevation: 8/12/16/20px radius scale and subtle shadows.
- Controls/layout: 44/48/52px controls, content widths, page padding.
- Motion: 150/200/300ms plus standard/out easing and reduced-motion collapse.
- Layering: content, header, popover, drawer, modal, toast, and skip-link z-index roles.

## Dark mode

Dark mode redefines the same `--twq-*` semantic names under `html[data-theme="dark"]`. Shared components do not contain a parallel dark stylesheet. Legacy dark-mode rules remain for unmigrated pages.

## Status mapping

| Product meaning | Foundation status |
|---|---|
| Active, completed, approved | `success` |
| Pending, attention, warning | `warning` |
| Rejected, failed, error | `danger` |
| Informational | `info` |
| Archived, inactive | `neutral` |
| Draft | `draft` |

Status components must include visible text; color is supplementary.

## Shared component API

The foundation in `static/css/design-system.css` provides:

- Buttons: `.twq-btn` and primary/secondary/ghost/danger variants; `.twq-icon-btn`.
- Forms: `.twq-field`, `.twq-label`, `.twq-control`, `.twq-choice`, validation states.
- Cards: `.twq-card` with KPI/action/document/status variants.
- Status: `.twq-status` with modifier or `data-status` semantics.
- Tables: `.twq-table-wrap`, `.twq-table`, actions and state regions.
- Modal shell: `.twq-modal` and backdrop/dialog/header/body/footer elements.
- States: `.twq-empty`, `.twq-loading`, `.twq-spinner`.
- Feedback: `.twq-alert` variants and `.twq-toast-region`.
- Pagination: `.twq-pagination`, link, and metadata elements.

The existing shared pagination partial adopts the new pagination hooks while retaining its legacy classes. Other templates migrate only during their own verified implementation phase.

## Inline-style migration baseline

The current repository scan finds 153 `<style>` blocks across 145 templates and 67 `style=` attributes. No page-owned block or attribute was removed in this phase.

The most repeated declarations inside template style blocks confirm that the safe extraction seam is the shared component layer: `display: flex` (646), `align-items: center` (518), `display: grid` (353), `display: inline-flex` (244), `justify-content: space-between` (163), and the 8/10/12px gap family (127/156/132). Heavy one-off font weights also recur (`700` through `950`) but are not bulk-rewritten because their hierarchy must be reviewed in page context.

Buttons, fields, cards, statuses, tables, modal shells, empty/loading states, alerts/toasts, and pagination now have canonical `.twq-*` replacements for future route-by-route migration. The first low-risk adoption is the shared pagination partial. A declaration is removed from a page only when its owning template is migrated and verified; mere similarity is not sufficient evidence.

## Responsive targets

- Mobile: base through 639px.
- Tablet/compact: 640–1023px.
- Desktop: 1024–1279px.
- Wide: 1280px and above.

Custom properties document 40rem, 64rem, and 80rem thresholds, but native media queries use the literal values because CSS custom properties are not valid media-query operands.

## Migration rules

1. Inspect the actual route, template inheritance, role and JavaScript contracts.
2. Add the relevant `.twq-*` hook beside the existing class; do not remove the legacy class yet.
3. Compare light/dark, RTL, 375px/1440px, focus, loading, empty, error, and disabled states.
4. Run relevant Django UI tests and browser checks.
5. Remove a legacy rule or alias only when usage is zero and the migrated route is verified.

## Deliberate non-goals in this phase

- No mass template redesign.
- No bulk deletion of legacy CSS or breakpoints.
- No broad extraction of the 145 template style blocks.
- No backend, model, form contract, route, permission, migration, PWA architecture, or Flutter changes.
- No framework or dependency addition.
