# BidBrain UI design contract (research-tool calm, glass surfaces)

Every page uses these primitives and tokens. Do not invent new colours, radii, shadows or one-off styles.

## Look and feel

* **A calm research tool (Sprig-like).** Warm neutral text (`--text` stone-900), small type (13-13.5px body,
  19px page titles, 14px card titles), and a **charcoal primary action** in a rounded rectangle (8px), not a pill.
  The secondary button is white with a hairline ring.
* **The app is one window.** The whole app sits in a frosted `.frame` with 18px corners over a warm canvas lit by
  three faint pastel blobs. The sidebar is translucent; the content side is a lighter `.main-panel`. The sidebar
  carries the wordmark, a black "Qualify a tender" action, and two nav groups split by a hairline.
* **Pastel category tiles** (`<IconTile tone>`: lavender, peach, teal, sage, rose, sand) mark a *type*: a page in
  its header, a tender's category, an explore row. Never on every card title.
* **Lavender is the accent** (`--kb`) for AI entry points (`.ai-panel`), links and the evidence path. Data bars
  use the soft bar palette (`--bar-1` terracotta, `--bar-2` teal grey, `--bar-3` sage, `--bar-4` lavender) on a
  `--bar-track`, as in `<ResultBar>`.
* **Tabs are filled**, not underlined: the active tab is a quiet grey rounded rectangle; tabs can carry an icon.
* **Glass:** cards are `.glass` (translucent fill, blur, a 1px ring and a 1px inner top highlight). Don't stack
  glass on glass; inner blocks use `.well`.
* **Dark mode** exists via `data-theme="dark"`; use only the tokens.

## Anti-"AI slop" rules (mandatory)

1. No gradient text and no emoji. Icon tiles only where they mark a type (see above), never as filler.
2. No UPPERCASE eyebrow labels above every block. Section titles are **sentence case**, 15px, semibold
   (`<Section title=…>`). Uppercase micro-labels are allowed only as table column headers.
3. Copy is functional and short, like a product, not a landing page. Don't repeat "the graph" in every sentence.
   Numbers and real data come first.
4. Use chips/badges only for real categorical values (a verdict, a category, a policy). Never wrap plain text in
   chips.
5. One accent colour (lavender). Semantic colours only for meaning: pass `--ok`, fail `--bad`, risk `--warn`. Pastel tiles mark types, bars show data.
6. Generous but consistent spacing: 4px grid, cards `p-5`, section gaps `gap-5`/`gap-6`. Align everything to the
   grid.
7. Icons: lucide-react only, `size={16}` and `strokeWidth={1.75}` next to 14px text, `currentColor`, no decorative
   icons beside every title.

## Tokens (globals.css)

`--bg` canvas, `--surface` (glass fill), `--surface-solid`, `--well` (inner flat block), `--line` hairline,
`--text`, `--text-2` secondary, `--text-3` tertiary, `--kb`, `--kb-ink`, `--kb-soft` (blue tint background), `--ok`,
`--ok-soft`, `--bad`, `--bad-soft`, `--warn`, `--warn-soft`, `--violet`, `--shadow-border`, `--shadow-border-hover`,
`--shadow-pop`.

Tailwind color utilities map to them: `bg-surface`, `bg-well`, `text-ink`, `text-ink-2`, `text-ink-3`, `text-kb`,
`text-kb-ink`, `bg-kb-soft`, `border-line`, `text-ok`, `text-bad`, `text-warn`, and so on.

## Primitives (`@/components/ui`)

| Primitive | Use |
|---|---|
| `<Card title? icon? tone? action? className? pad?>` | the glass card. `title` = sentence-case section title; `icon`+`tone` add a type tile; `action` = right-aligned node |
| `<IconTile tone size?>` | pastel rounded-square type marker |
| `<ResultBar label value max? right? tone? display?>` | "84% · Home ....... 26 responses" result bar |
| `<Section title subtitle? action?>` | a heading row inside a page without a card |
| `<Tabs tabs={[{id,label,count?,icon?,disabled?}]} value onChange />` | filled tabs |
| `<Button variant="primary\|accent\|secondary\|ghost" size="sm\|md" static? …>` | 8px-radius buttons; press scale 0.96 |
| `<Badge tone="neutral\|blue\|ok\|bad\|warn\|violet">` | soft pill for categorical values |
| `<Verdict v="PASS\|FAIL\|AT_RISK\|DECLARE\|BID\|NO-BID\|BID WITH FIXES\|PROVEN" size? />` | verdict pill with dot |
| `<Stat label value sub? tone?>` | big number with a label; inside a card grid, no border of its own |
| `<Avatar name size?>` | initials avatar (deterministic color) |
| `<Empty icon? title text?>` | empty-state block |
| `<CodeCell code lang="cypher" label?>` | notebook-style cell (gutter label "Cypher") |
| `<Markdown>{md}</Markdown>` | memo / explanation rendering |
| `<Spinner label?>` | inline progress |
| `<EvidenceList items max?>` | document citations; opens the PDF viewer at the page |
| `useOpenDoc()` | open the PDF viewer programmatically |
| `<Table>` CSS: `.kt` on `<table>` | leaderboard / data tables (sticky header, hairline rows, hover row tint) |

Page shell: `PageHeader({title, subtitle?, meta?, actions?, icon?, tone?})` from `@/components/Shell`. It
renders an optional type tile, the title, a one-line subtitle and a meta line of small facts; the page puts
`<Tabs>` directly under it where the page has sections.

Graph: `GraphCanvas` from `@/components/GraphCanvas` (props unchanged: `data, highlight, height, onNodeClick,
emptyHint, showAllLabels`). It is theme-aware; wrap it in a `Card pad={false}` with `className="overflow-hidden"`.

## Motion

* Buttons press to `scale: 0.96` over 150ms ease-out (built into `Button`).
* Hover on rows and cards: `background-color` / `box-shadow` only, 150ms.
* Page entrance: none (no staggered marketing animations). Spinners only where work is in flight.
* Anything that moves, scales or blurs goes behind `motion-safe:`.
