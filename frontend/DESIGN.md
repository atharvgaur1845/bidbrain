# BidBrain UI contract: light enterprise console

Operations software: dense, legible, calm. Every page uses these tokens and primitives; no one-off colours, radii or shadows.

## Look
* Warm near-white canvas (`--canvas` #f7f7f5), white cards with a 1px `--line` border, charcoal text (`--text`).
* One accent: navy `--kb` / `--accent` (#2b4c9b). Primary buttons, active tab underline, active nav icon, links (`text-kb-ink`).
* Shadows: only `--shadow-xs` on cards and `--shadow-pop` on dialogs/drawers. No glass, blur, blobs, gradients, glow.
* Radius: 4px badges, 6px buttons/inputs, 8px cards (Tailwind `rounded-xl/2xl/3xl` are clamped to 8px). `rounded-full` only for dots/avatars.
* Light only (no dark theme, no toggle).

## Status colours (one meaning each) — `STATUS` map in `ui.tsx`
| Tone | Token | Statuses |
|---|---|---|
| ok (green) | `--ok` | PASS, BID, PROVEN, executed, resolved |
| bad (red) | `--bad` | FAIL, NO-BID, rejected |
| warn (amber) | `--warn` | AT_RISK, BID WITH FIXES, ripple |
| review (violet) | `--violet` | NEEDS REVIEW, open, conflict |
| info (blue) | `--info` | DECLARE, approved |
| neutral (slate) | `--neutral` | pending, superseded, unknown |
Each has `-soft` (fill) and `-line` (border). Never use a status colour for decoration.

## Type scale
micro 11 (column headers, nav groups) · caption 12 (meta) · small 12.5 (dense cells, badges) · body 13.5 · title 14 (card titles) · heading 16 · page 20 (h1). Utilities: `text-micro … text-page`. Numbers: `.num` (tabular figures). IDs/code: `.mono`. Sentence case everywhere; no uppercase eyebrows, no emoji.

## Spacing / layout
4px grid. Cards `p-4`, header row `min-h-11 px-4` with a bottom border. Page gaps `gap-4`. Shell: 220px left nav (lg+), drawer below lg, 48px top bar (search, Reset graph). Content max 1440px, 16px gutter on phones.

## Focus and states
`:focus-visible` = 2px navy outline, offset 2px (global, base layer). Hover = background `surface-2`/`well` or border `line-strong`, 100-120ms. Selected rows: `aria-selected` → `--kb-soft`. Disabled: 50% opacity, `not-allowed`. Loading: `Button loading`, `Spinner`, `Skeleton`, `SkeletonRows`.

## CSS classes (components layer, so utilities override them)
`.glass`/`.card` card · `.glass-pop` dialog · `.well` inset block · `.interactive-card` · `.ai-panel` (plain panel now) · `.field` input · `.kt` table (sticky header, dense rows; `.n`/`[data-num]` right-aligned tabular; `.kt.dense`) · `.md` markdown · `.scroll-thin` · `.skeleton` · `.spin`.

## Primitives (`@/components/ui`)
| Primitive | API |
|---|---|
| `Card` / `Panel` | `title? subtitle? icon? action? footer? pad? className? id?` (`tone` accepted, ignored) |
| `Section` | `title subtitle? action?` |
| `Tabs` | `tabs=[{id,label,count?,icon?,disabled?}] value onChange className?` underline, arrow/Home/End keys |
| `Button` | `variant=primary\|accent\|secondary\|ghost\|danger size=xs\|sm\|md\|lg loading? static?` |
| `ButtonLink`, `buttonClass()` | link styled as button |
| `StatusBadge` | `status size=sm\|md\|lg dot? label?`; `statusInfo(s)`, `STATUS`, `TONE_DOT` |
| `Verdict` | `v size?` → StatusBadge |
| `Badge` | `tone=neutral\|ok\|bad\|warn\|review\|info\|accent` (legacy `blue`,`violet`) |
| `Table`, `Th`, `Td`, `Tr` | `Table maxHeight? dense? label?`; `Th align width`; `Td align numeric muted`; `Tr selected onClick` |
| `KeyValue` | `items=[[label,value]] \| [{label,value,mono}] cols=1\|2\|3` |
| `Timeline`, `TimelineItem` | `title time? status? icon? children? last?` |
| `EmptyState` / `Empty` | `icon? title text? action? compact?` |
| `ErrorState` | `title? text? onRetry? compact?` |
| `Notice` | `tone title? children? action?` inline banner |
| `Skeleton`, `SkeletonRows` | `w h` / `rows` |
| `Stat` | `label value sub? tone?` |
| `IconTile` | neutral square (`tone` ignored) |
| `ResultBar` | `label value max? right? tone? display?` thin bar, `--bar-1` accent then slates |
| `Avatar`, `Spinner`, `CodeCell`, `Markdown`, `EvidenceList`, `useOpenDoc`, `DocProvider` | unchanged APIs |

Shell: `PageHeader({title, subtitle?, meta?, actions?})` (`icon/tone/eyebrow` accepted, not drawn). Graph: `GraphCanvas` (props unchanged), restrained categorical `LABEL_COLORS`, path colour `--graph-path`.
