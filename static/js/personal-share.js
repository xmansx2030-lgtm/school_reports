(() => {
  document.querySelectorAll('[data-personal-share-print]').forEach((button) => {
    button.addEventListener('click', () => window.print());
  });
  const copyButton = document.querySelector('[data-personal-share-copy]');
  const urlInput = document.getElementById('personalShareUrl');
  const status = document.getElementById('personalShareCopyStatus');
  if (!copyButton || !urlInput || !status) return;
  copyButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(urlInput.value);
      status.textContent = 'نُسخ الرابط.';
    } catch (_) {
      urlInput.focus();
      urlInput.select();
      status.textContent = 'حدد الرابط، ثم انسخه من لوحة المفاتيح.';
    }
  });
})();
