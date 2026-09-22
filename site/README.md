# Benchmark article

A static GitHub Pages article, with no build dependencies.

- Edit `index.html` for the introduction, Astra video and interpretation, Pareto frontier, and conclusion.
- Edit `style.css` for typography and layout. Didot and Helvetica Neue use local system fonts with cross-platform fallbacks.
- `chart-toggle.js` switches the leading figure between **Best** (default) and **Mean**, including its alt text, caption, and full-size links. With JavaScript disabled, Best remains visible and a link opens Mean.
- `assets/progress.png` is the annotated best-run chart; `assets/progress-mean.png` is the matching unannotated mean chart. Both are 3072 × 1728. The separate Astra achievement-key figure has been removed.
- Preview with `python3 -m http.server 8000 --directory site`.
- Push changes to `site/` on `main` to publish, or run “Publish benchmark article” manually in Actions.

The repository Pages source is **GitHub Actions**. The site URL is https://massiminoe.github.io/minetrials/ . Only `site/` is uploaded, never runtime state, credentials, or transcripts.

Figure 1 uses 60 runs / 13 cohorts in both views. Best chooses the highest strict one-hour count, then the earliest final counted advancement, then run ID. Mean weights runs equally. Astra’s selected run is `astra-low-20260917-three-t3`, with 24 advancements. The embedded recording is https://youtu.be/ntVf2DUeaBg; commentary caption timing is approximate.

Figure 2 covers 53 runs / 11 priced cohorts, with Cursor excluded and Codex valued at standard short-context API rates. Historical configurations differ, Astra low reasoning is reconstructed, and Codex long-context pricing applicability is unverified. These qualifications are retained in the article’s expandable methodology note and cost discussion.

Charts were prepared in the local scratch chart workspace on 22 September 2026 using the release-audit data. Raw run artifacts and plotting scripts are not published in `site/`. The dataset and traces are linked at https://huggingface.co/datasets/mxls/MineTrials .

The page retains its working-draft label pending final cohort and pricing review. The compact masthead uses a text wordmark; the unused local logo screenshot is not part of this update. Minecraft textures and provider marks remain the property of their respective owners; the repository’s original-code license does not license those assets.
