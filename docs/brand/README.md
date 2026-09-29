# Echo: visual reference

Visual work on Echo (the web UI, the mark, the Telegram avatar) starts
here.

## Source

The owner's Pinterest mood board **"cybersyn"**:
<https://www.pinterest.com/marystue/cybersyn/> (58 pins as of
2026-09-29). The notes below were written from the pins themselves.
If the board grows in a new direction, update this page with it.

The name is a nod to cybernetics (Ross Ashby's *An Introduction to
Cybernetics* is pinned), but the board isn't about the Chilean
operations room. It's about the machine as something almost sacred:
"divine machinery", "Computers = Electrical Spiritual Devices".

## The aesthetic

- **Palette.** Mostly black and near-black, with stark white and 1-bit
  dithered or halftone greys. One colour carries the signal: **phosphor
  green**, from CRT terminals, an ECG monitor, green "Online" labels over
  a block of flats, and circuit boards. Blue shows up rarely (a BSOD
  wall, a Win98 selection bar, a CRT glow) and never as the lead.
- **Imagery.** Angels and machines together; tangled cables; CRT screens;
  circuit boards; posthuman figures; surveillance cameras and eyes.
- **Diagrams.** Annotated technical drawings, node-and-arrow flowcharts,
  orbit plots, a CCRU numogram, "Everything is connected", lab notes
  pinned together with lines. The drawing is often the whole picture.
- **Type.** Terminal monospace and pixel CRT type ("ARE YOU EXPANDING
  YOUR MIND?", "What you see, you become."). Heavy grotesques on
  posters ("DE-HUMANIZATION", "Discredit the witnesses"). Wide,
  letterspaced capitals. A Times-style serif for the academic or occult
  register. ASCII art.
- **Texture.** Dither noise, scanlines, glitch and datamosh, Win98 window
  chrome.
- **Mood.** Late at night, alone with the screen: the Wired from Serial
  Experiments Lain. Transcendence through the machine, with an edge of
  being watched.

## What Echo takes from it

Echo is a private companion, so it takes the calm half of the board
(the terminal, the diagram sheet, the single green light) and leaves
the dread.

- Black-and-white ground in neutral greys (not warm ones), with one
  phosphor-green signal.
- Hard, square edges, and flat surfaces where a hairline carries the
  edge instead of a soft shadow.
- System text (times, states, counts) in the mono face, like a terminal
  line.
- Charts drawn as a diagram would draw them: plain bars, nodes, arrows.

What it doesn't take:

- Glitch, scanlines or datamosh on anything you read. It costs
  legibility, and motion is a problem for some eyes.
- Surveillance imagery. Echo holds private conversations, and nothing
  in it should make the user feel watched.
- Gore, anxiety posters, or religious iconography in the interface.

The text is Russian first, so any typeface has to cover Cyrillic
properly.

## The mark

Three rings, each smaller and fainter, overlapping a little: a signal
and its echo. It keeps the original mark's geometry, redrawn in
phosphor green (`#5fe08a`) on a black square (`#0a0a0a`). Two
variations were tried and turned down: a solid first node, and a
feedback-loop wire with an arrow over the circles.
The geometry is on a 32-unit grid and is shared by:

- `app/web/static/icon.svg`: the favicon, on its black tile, which
  reads the same on light and dark browser tabs.
- `docs/brand/echo-avatar.svg`: the Telegram avatar, scaled to fit
  Telegram's round crop. `echo-avatar.png` is the same SVG rendered at
  exactly 640×640 (set it with BotFather's `/setuserpic`).
- `_MARK` in `app/web/oauth.py`: a one-colour copy in `currentColor`,
  with no tile, so it takes the page's accent.

Change all three together.

## Web UI palette

The web UI's palette is **wired**, the whole board: a black screen with
phosphor green by night, a white diagram sheet with black ink by day.
It uses neutral greys (not warm ones), square corners everywhere, and
flat surfaces. It lives in the token blocks at the top of
`app/web/static/app.css`, and the OAuth page (`_STYLE` in
`app/web/oauth.py`) mirrors it, so change both together. (A gentler
`phosphor` look, the previous palette with a green accent, was tried as
an option and dropped when wired became the default.)

Every text and accent pair is at least 4.5:1 (WCAG AA). `--danger`
stays red. Red and green are hard to tell apart for colour-blind eyes,
which is acceptable only because the UI never shows state by colour
alone. Keep it that way.

| Token | Light | Dark |
|---|---|---|
| `--bg` | `#f4f4f1` sheet | `#0a0a0a` screen |
| `--surface` | `#ffffff` | `#121412` |
| `--text` | `#0a0a0a` (18.0:1) | `#e6e6e1` (15.8:1) |
| `--text-muted` | `#555753` (6.6:1) | `#9a9a94` (7.0:1) |
| `--accent` | `#146c34` (5.9:1 on `--bg`) | `#5fe08a` phosphor (11.8:1) |
| `--accent-text` | `#ffffff` (6.5:1) | `#0a0a0a` (11.8:1) |
| `--radius`, `--radius-card`, `--radius-pill` | `0` | same |
| Shadows | none on cards (the hairline carries the edge); popovers keep a short one | same |

Only dots and avatars stay round (`border-radius: 50%`); every other
corner goes through the radius tokens.

## Next steps (not done yet)

- **Fonts with Cyrillic.** Geist Mono is Latin-only, so Russian system
  text falls back to Manrope. A mono with Cyrillic (e.g. JetBrains Mono
  or IBM Plex Mono) for `--font-mono`, and possibly a stricter grotesque
  for `--font`, can be vendored through `_FONT_PACKAGES` in
  `scripts/vendor_web.py` like the current fonts.
