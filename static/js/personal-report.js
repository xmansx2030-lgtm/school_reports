/* Compatibility for drafts saved by the former personal report editor.
 * Current authoring and evidence interactions use the school editor scripts.
 */
(() => {
  'use strict';

  const completed = document.querySelector('[data-personal-draft-complete]');
  if (completed) {
    try {
      const pending = sessionStorage.getItem('personal-report-pending');
      if (pending && [completed.dataset.draftCreateKey, completed.dataset.draftEditKey].includes(pending)) {
        localStorage.removeItem(pending);
        sessionStorage.removeItem('personal-report-pending');
      }
    } catch (_) { /* storage unavailable */ }
  }

  const form = document.getElementById('report-form');
  if (!form || !form.classList.contains('personal-report-form')) return;

  const yearField = form.elements.namedItem('academic_year');
  const yearContext = document.getElementById('personalYearContext');
  if (yearField && yearContext) {
    const serverYear = yearField.value;
    const showYear = () => {
      // IndexedDB drafts can contain a year from an older session.
      // The active personal year comes from the server and cannot be changed here.
      if (yearField.value !== serverYear) yearField.value = serverYear;
      yearContext.textContent = serverYear.trim() || '—';
    };
    yearField.addEventListener('input', showYear);
    yearField.addEventListener('change', showYear);
  }

  const legacyKey = form.dataset.legacyPersonalDraftKey;
  const banner = document.getElementById('draftBanner');
  const stamp = document.getElementById('draftTime');
  if (!legacyKey || !banner) return;

  let draft;
  try {
    draft = JSON.parse(localStorage.getItem(legacyKey) || 'null');
  } catch (_) {
    draft = null;
  }
  if (!draft || !draft.values || form.querySelector('[data-server-error-summary]')) return;

  const names = {
    selection_enabled: 'section_selection_enabled',
    show_goals: 'show_goal',
    goals: 'goal',
    description: 'idea',
    implementation: 'implementation_method',
  };
  const dropLegacy = () => {
    try { localStorage.removeItem(legacyKey); } catch (_) { /* storage unavailable */ }
    banner.hidden = true;
  };

  if (stamp && draft.savedAt) {
    stamp.textContent = 'حُفظت في ' + new Date(draft.savedAt).toLocaleString('ar-SA') +
      ' · أعد إرفاق الملفات عند الحاجة.';
  }
  banner.hidden = false;
  document.getElementById('draftRestore')?.addEventListener('click', () => {
    for (const [oldName, stored] of Object.entries(draft.values)) {
      const name = names[oldName] || oldName;
      const field = form.elements.namedItem(name);
      if (!field || ['file', 'hidden'].includes(field.type)) continue;
      if (field.type === 'checkbox') field.checked = Boolean(stored);
      else field.value = stored;
      field.dispatchEvent(new Event('input', { bubbles: true }));
      field.dispatchEvent(new Event('change', { bubbles: true }));
    }
    dropLegacy();
  });
  document.getElementById('draftDiscard')?.addEventListener('click', dropLegacy);
  form.addEventListener('tawtheeq:submit-success', dropLegacy, { once: true });
})();
