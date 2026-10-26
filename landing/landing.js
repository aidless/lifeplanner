const sheet = document.getElementById('settlement');
if (sheet) {
  const buttons = Array.from(sheet.querySelectorAll('[data-mode-btn]'));
  const setMode = (mode) => {
    sheet.dataset.mode = mode;
    buttons.forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.modeBtn === mode));
    });
  };
  buttons.forEach((button) => {
    button.addEventListener('click', () => setMode(button.dataset.modeBtn));
  });
}

const copyButton = document.querySelector('[data-copy]');
if (copyButton) {
  const original = copyButton.textContent;
  copyButton.addEventListener('click', async () => {
    const text = copyButton.dataset.copy || '';
    try {
      await navigator.clipboard.writeText(text);
      copyButton.textContent = '已复制';
    } catch {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      copyButton.textContent = ok ? '已复制' : '复制失败';
    }
    window.setTimeout(() => { copyButton.textContent = original; }, 1800);
  });
}
