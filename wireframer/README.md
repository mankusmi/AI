# Dashboard Sketchpad

A single-file wireframing tool for Power BI report conversations with the business. Open `index.html` in a browser; no build step.

- **Build**: drag visuals (cards, KPIs, charts, tables, maps, slicers, filter pane) onto a 16:9 canvas, move/resize with grid snap, set title, data fields and sample values.
- **Discuss**: numbered badges and a conversation list. Mark each visual Draft / Open question / Agreed / Dropped and record the decision.
- **Export**: developer spec (Markdown, includes open questions) or project JSON (re-importable). Autosaves to the browser.
- Multi-page reports, templates (Sales, Operations), undo/redo, dark mode.

## Run it locally and privately

Double-click `launch.bat` (Windows) or run `./launch.sh` (macOS/Linux), or just open `index.html` in any browser. No server, install or account is needed.

- The page makes no network requests. It has no external fonts or scripts, and a Content-Security-Policy blocks all outbound connections.
- Projects autosave in your browser's local storage on your machine. Use **Export > Save as file** and **Open file** to keep and reload `.json` projects.
