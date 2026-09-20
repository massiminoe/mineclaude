# Benchmark article

A static GitHub Pages article, with no build dependencies.

- Edit `index.html` for copy, section order, captions, and links. `EDIT` comments mark the major sections.
- Edit `style.css` for typography and layout. Didot and Helvetica Neue use local system fonts, with cross-platform fallbacks; no commercial font files are distributed.
- Replace files in `assets/` when regenerating figures. Current figures were prepared in the scratch chart workspace on 20 September 2026. Plot code and raw run artifacts are not published here.
- Preview with `python3 -m http.server 8000 --directory site` from the repository root.
- Push changes to `site/` on `main` to publish, or run “Publish benchmark article” manually in Actions.

The repository Pages source must be set to **GitHub Actions**. The site URL is https://massiminoe.github.io/mineclaude/ . The workflow uploads only `site/`, never runtime state, credentials, or transcripts.

The page is deliberately labeled a working draft. Remaining editorial work: refine motivation and interpretation, add the published video URL, finalize cohort selection, and verify Codex long-context pricing applicability. Add a video embed only once its public URL is available; there is currently no broken or placeholder player.

Figure 1: 60 runs / 13 cohorts. Figure 2: video-selected Astra run `astra-low-20260917-three-t3`, 24 advancements. Figure 3: 53 runs / 11 priced cohorts, with Cursor excluded and Codex valued at standard short-context API rates. Retain the qualifications when editing.

Minecraft textures and model-provider marks in the figures remain the property of their respective owners; the repository's original-code license does not license those assets.
