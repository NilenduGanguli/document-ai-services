# Executive briefing deck

`Document-Intelligence-Executive-Briefing.pptx` — a 16-slide, 16:9 deck for higher management
covering the problem, the platform, the system architecture, each flow, security/compliance,
scale, and roadmap. "Midnight Executive" palette (navy / ice-blue / white), speaker notes on
every slide.

## Rebuild

```bash
cd presentation
npm install pptxgenjs          # build-only dep (git-ignored)
node build_deck.js             # -> Document-Intelligence-Executive-Briefing.pptx
```

## Diagrams

Slide diagrams live in `assets/diagrams/*.png`, rendered from `diagrams_src/` with
[`@mermaid-js/mermaid-cli`](https://github.com/mermaid-js/mermaid-cli) (`mmdc`):

```bash
npx -y @mermaid-js/mermaid-cli -i diagrams_src/<name>.mmd -o assets/diagrams/<name>.png \
    -b white -w 2600 -s 2
```

- `*.simple.mmd` — management-simplified diagrams used on the slides (architecture, ocr, search,
  merge, datamodel). Fewer, larger nodes so labels stay readable when projected.
- `*.mmd` / `*.md` — the detailed diagrams extracted from `docs/`. The full, technical versions
  render in the documentation set (`docs/` and `docs/pdf/`); the deck uses the simplified ones.

`build_deck.js` carries each PNG's pixel dimensions in `DIMS` and centers every image at the
correct aspect ratio, so re-rendered diagrams only need their `DIMS` entry updated if their shape
changes.
