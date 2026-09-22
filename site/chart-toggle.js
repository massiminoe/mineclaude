(() => {
  const controls = document.querySelector('.chart-controls');
  const chart = document.getElementById('progress-chart');
  const imageLink = document.getElementById('progress-image-link');
  const fullSize = document.getElementById('progress-full-size');
  const caption = document.getElementById('progress-caption');
  if (!controls || !chart || !imageLink || !fullSize || !caption) return;

  const views = {
    best: {
      src: 'assets/progress.png',
      alt: 'Best-run cumulative Minecraft advancements for 13 model–harness combinations over one hour. GPT-6 Astra with Codex reaches 24; icons mark all 24 advancements. Other setups peak at 18.',
      caption: 'Best run for each model–harness combination in a fixed-seed survival world. Ties use the earliest final advancement. Icons follow Astra’s selected run.',
      linkLabel: 'Open the best-run progress chart at full resolution',
    },
    mean: {
      src: 'assets/progress-mean.png',
      alt: 'Mean cumulative Minecraft advancements over one hour across 60 runs and 13 model–harness combinations. GPT-6 Astra with Codex leads at 22.4 advancements across five runs. No individual-run annotations are shown.',
      caption: 'Mean progress across repeated runs for each model–harness combination in the same fixed-seed survival world. Every run has equal weight; 60 runs across 13 combinations.',
      linkLabel: 'Open the mean progress chart at full resolution',
    },
  };

  for (const button of controls.querySelectorAll('button')) {
    button.addEventListener('click', () => {
      const view = views[button.dataset.chart];
      if (!view) return;
      chart.src = view.src;
      chart.alt = view.alt;
      imageLink.href = fullSize.href = view.src;
      imageLink.setAttribute('aria-label', view.linkLabel);
      caption.textContent = view.caption;
      for (const option of controls.querySelectorAll('button')) {
        option.setAttribute('aria-pressed', String(option === button));
      }
    });
  }
  controls.hidden = false;
})();
