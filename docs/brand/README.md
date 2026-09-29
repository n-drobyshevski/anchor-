# Echo: visual reference

Visual work on Echo (the web UI, the mark, the Telegram avatar) starts
here.

## Source

The mood board is the owner's Pinterest board **"cybersyn"**. It is a
secret board, and this repository is public, so it is **not linked
here on purpose**. Ask the owner for access, and never commit its link
(the share link carries an invite code). The description below is
written from Project Cybersyn itself (Chile, 1971–73, Stafford Beer's
cybernetic management project; the operations room was designed by Gui
Bonsiepe's team), which is what the board collects.

## The aesthetic

- **The operations room.** A hexagonal room with dark walnut walls and
  seven white fiberglass swivel chairs upholstered in orange, facing
  rear-projected screens. Nothing on the desks: every control sits
  in the armrests.
- **Diagrams, not decoration.** Flat, bold pictograms and hand-drawn
  flow and feedback diagrams (Beer's Viable System Model). Data shows
  up as simple bars, lights and indicators.
- **Type.** Helvetica-style neo-grotesque. Uppercase labels, tight
  hierarchy, generous space.
- **Mood.** Calm control: "a room for deciding". 1970s modernism,
  optimistic and exact, never kitsch.

## What Echo takes from it

- One warm orange signal colour on neutral ground: paper and ink by day,
  walnut and fiberglass white by night.
- Squarer corners, and flat surfaces where a hairline carries the edge
  instead of a soft shadow.
- Metadata (times, counts, states) set small and uppercase in the mono
  face, like a panel label.
- Charts as plain bars and dots, the way the room's screens showed them.

What it doesn't take: retro pastiche, film grain or textures, or
skeuomorphic chrome. The text is Russian first, so any typeface has to
cover Cyrillic properly.

## Web UI looks

`app/web/static/app.css` keeps today's palette as the default and adds
two opt-in looks. They only reassign existing tokens. Try them in the
web UI:

- `/?look=signal`
- `/?look=opsroom`
- `/?look=default` goes back (the choice is remembered in the browser).

Every text and accent pair below is at least 4.5:1 (WCAG AA). `--danger`
stays red in both looks, so an error never reads as the orange accent.

### `signal`: the smallest step

Today's warm neutrals, with the stone accent swapped for Cybersyn
orange. Only `--accent`, `--focus`, `--dot-ok` and `--chart-bar` change.

| Token | Light | Dark |
|---|---|---|
| `--accent` | `#b8430f` (5.15:1 on `--bg`) | `#f07f3c` (6.5:1 on `--bg`) |
| `--accent-text` | `#ffffff` (5.46:1) | `#292524` (5.64:1) |

### `opsroom`: the whole room

| Token | Light | Dark |
|---|---|---|
| `--bg` | `#f4efe6` paper | `#161412` walnut |
| `--surface` | `#fbf8f2` | `#201d1a` |
| `--text` | `#1a1714` (15.6:1) | `#ece6da` fiberglass (14.8:1) |
| `--text-muted` | `#5c554d` (6.4:1) | `#a39a8e` (6.6:1) |
| `--accent` | `#b8430f` (4.8:1 on `--bg`) | `#f07f3c` (6.8:1) |
| `--accent-text` | `#ffffff` (5.46:1) | `#161412` (6.8:1) |
| `--radius` / `--radius-card` | `4px` / `6px` | same |
| Shadows | none on cards (the hairline carries the edge); popovers keep a short one | same |

## Next steps (not done yet)

- **A grotesque with Cyrillic** (e.g. Golos or Inter), vendored through
  `_FONT_PACKAGES` in `scripts/vendor_web.py` like the current fonts,
  as `--font` for `opsroom`.
- **Tokenize the remaining hard-coded radii** in `app.css` (about ten
  `border-radius` values: bubbles, chips, the composer), so a look
  can square them too.
- **The mark and avatar**: redraw `app/web/static/icon.svg` and
  `echo-avatar.svg`/`.png` here as a flat pictogram in the orange.
- **Pick a default**: once a look has been lived with, promote it to
  `:root` and drop the switch.
