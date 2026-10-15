# BidBrain UI design contract (Kaggle-style, glass surfaces)

Every page uses these primitives and tokens. Do not invent new colors, radii, shadows or one-off styles.

## Look and feel

* **Kaggle's product language.** The canvas is light and calm, with a white glass surface over a softly tinted
  background. Use **Kaggle blue** for actions, links and the evidence path: `--kb` = `#20BEFF` brand, `--kb-ink` =
  `#0086C3` for text and links. The primary button is a **black pill**, the secondary is a white pill with a
  hairline. Use **underline tabs** for in-page sections, **leaderboard tables** for rankings, and
  **notebook-style cells** for code and Cypher. People get **initials avatars**.
* **Glass.** Cards are frosted (`.glass`): a translucent white fill, backdrop blur, a 3-layer shadow ring (not a
  heavy border), and a 1px inner top highlight. The page background has two faint radial tints (blue, violet).
  Don't stack glass on glass; inner blocks use `.well` (a flat, subtle fill).
* **Dark mode** exists via `data-theme="dark"`. Use only the tokens, never hard-coded hex values in pages.

## Anti-"AI slop" rules (mandatory)

1. No gradient text, emoji, sparkle icons or "✨". No icons inside colored circles as decoration.
2. No UPPERCASE eyebrow labels above every block. Section titles are **sentence case**, 15px, semibold
   (`<Section title=…>`). Uppercase micro-labels are allowed only as table column headers.
3. Copy is functional and short, like a product, not a landing page. Don't repeat "the graph" in every sentence.
   Numbers and real data come first.
4. Use chips/badges only for real categorical values (a verdict, a category, a policy). Never wrap plain text in
   chips.
5. One accent color (Kaggle blue). Semantic colors only for meaning: pass `--ok`, fail `--bad`, risk `--warn`.
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
| `<Card title? action? className? pad?>` | the glass card. `title` = sentence-case section title; `action` = right-aligned node |
| `<Section title subtitle? action?>` | a heading row inside a page without a card |
| `<Tabs tabs={[{id,label,count?}]} value onChange />` | Kaggle underline tabs |
| `<Button variant="primary\|accent\|secondary\|ghost" size="sm\|md" static? …>` | pill buttons; press scale 0.96 |
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

Page shell: `PageHeader({title, subtitle?, meta?: ReactNode, actions?: ReactNode})` from `@/components/Shell`. It
renders the title row like a Kaggle competition header (title, a one-line subtitle, a meta line of small facts),
and the page puts `<Tabs>` directly under it where the page has sections.

Graph: `GraphCanvas` from `@/components/GraphCanvas` (props unchanged: `data, highlight, height, onNodeClick,
emptyHint, showAllLabels`). It is theme-aware; wrap it in a `Card pad={false}` with `className="overflow-hidden"`.

## Motion

* Buttons press to `scale: 0.96` over 150ms ease-out (built into `Button`).
* Hover on rows and cards: `background-color` / `box-shadow` only, 150ms.
* Page entrance: none (no staggered marketing animations). Spinners only where work is in flight.
* Anything that moves, scales or blurs goes behind `motion-safe:`.
