(() => {
  const completed = document.querySelector('[data-personal-draft-complete]');
  if (completed) {
    try {
      const pending = sessionStorage.getItem('personal-report-pending');
      if (pending && [completed.dataset.draftCreateKey, completed.dataset.draftEditKey].includes(pending)) {
        localStorage.removeItem(pending);
        sessionStorage.removeItem('personal-report-pending');
      }
    } catch (_) {
      // Storage is optional; the saved report is already available on the server.
    }
  }
  const form = document.querySelector('.personal-report-form');
  if (!form) return;
  const options = Array.from(form.querySelectorAll('[data-section-option]'));
  const summaryTitle = form.querySelector('#personalSummaryTitle');
  const summaryCategory = form.querySelector('#personalSummaryCategory');
  const summaryYear = form.querySelector('#personalSummaryYear');
  const summarySections = form.querySelector('#personalSummarySections');
  const title = form.querySelector('[name="title"]');
  const category = form.querySelector('[name="category"]');
  const year = form.querySelector('[name="academic_year"]');
  const updateSummary = () => {
    if (summaryTitle) summaryTitle.textContent = title?.value.trim() || '—';
    if (summaryCategory) summaryCategory.textContent = category?.value.trim() || '—';
    if (summaryYear) summaryYear.textContent = year?.value.trim() || '—';
    if (summarySections) summarySections.textContent = String(options.filter((option) => option.querySelector('input')?.checked).length);
  };
  [title, category, year].forEach((control) => control?.addEventListener('input', updateSummary));
  options.forEach((option) => {
    const control = option.querySelector('input');
    if (!control) return;
    const update = () => {
      option.classList.toggle('is-selected', control.checked);
      updateSummary();
    };
    control.addEventListener('change', update);
    update();
  });
  for (const section of form.querySelectorAll('[data-personal-section]')) {
    const control = form.querySelector(`[name="${section.dataset.personalSection}"]`);
    if (!control) continue;
    const update = () => { section.hidden = !control.checked; };
    control.addEventListener('change', update);
    update();
  }
  updateSummary();
  const reportDate = form.querySelector('[name="report_date"]');
  const reportDay = form.querySelector('#personalReportDay');
  const reportHijri = form.querySelector('#personalReportHijri');
  const updateDate = () => {
    const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(reportDate?.value || '');
    const date = parts ? new Date(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3])) : null;
    if (reportDay) reportDay.textContent = date && !Number.isNaN(date.getTime())
      ? new Intl.DateTimeFormat('ar-SA', { weekday: 'long' }).format(date) : '';
    if (reportHijri) reportHijri.textContent = window.TawtheeqHijri
      ? window.TawtheeqHijri.echo(reportDate?.value || '') : '';
  };
  reportDate?.addEventListener('change', updateDate);
  updateDate();

  const draftKey = form.dataset.draftKey;
  const draftBanner = form.querySelector('#personalDraftBanner');
  const draftTime = form.querySelector('#personalDraftTime');
  const draftSaved = form.querySelector('#personalDraftSaved');
  const draftControls = Array.from(form.elements).filter((control) =>
    control.name && !control.name.startsWith('evidence-') &&
    !['file', 'hidden', 'submit', 'button'].includes(control.type)
  );
  let draft = null;
  let draftTimer;
  try {
    draft = JSON.parse(localStorage.getItem(draftKey) || 'null');
  } catch (_) {
    draft = null;
  }
  if (draft && draft.values && !form.querySelector('.report-server-errors')) {
    draftBanner.hidden = false;
    if (draftTime && draft.savedAt) {
      draftTime.textContent = 'حُفظت في ' + new Date(draft.savedAt).toLocaleString('ar-SA') + ' · الملفات تحتاج إعادة إرفاق.';
    }
  }
  const saveDraft = () => {
    if (!draftKey) return;
    const values = {};
    draftControls.forEach((control) => {
      values[control.name] = control.type === 'checkbox' ? control.checked : control.value;
    });
    try {
      localStorage.setItem(draftKey, JSON.stringify({ values, savedAt: Date.now() }));
      if (draftSaved) draftSaved.hidden = false;
    } catch (_) {
      // Private browsing and storage quotas can disable local drafts.
    }
  };
  const queueDraft = () => {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraft, 700);
  };
  form.addEventListener('input', queueDraft);
  form.addEventListener('change', queueDraft);
  form.querySelector('#personalDraftRestore')?.addEventListener('click', () => {
    draftControls.forEach((control) => {
      if (!Object.prototype.hasOwnProperty.call(draft.values, control.name)) return;
      if (control.type === 'checkbox') control.checked = Boolean(draft.values[control.name]);
      else control.value = draft.values[control.name];
      control.dispatchEvent(new Event('input', { bubbles: true }));
      control.dispatchEvent(new Event('change', { bubbles: true }));
    });
    draftBanner.hidden = true;
    updateDate();
  });
  form.querySelector('#personalDraftDiscard')?.addEventListener('click', () => {
    try { localStorage.removeItem(draftKey); } catch (_) { /* unavailable storage */ }
    draftBanner.hidden = true;
  });
  form.addEventListener('submit', () => {
    clearTimeout(draftTimer);
    saveDraft();
    try { sessionStorage.setItem('personal-report-pending', draftKey); } catch (_) { /* unavailable storage */ }
  });
  const add = form.querySelector('#personalEvidenceAdd');
  const template = form.querySelector('#personalEvidenceTemplate');
  const rows = form.querySelector('#personalEvidenceRows');
  const total = form.querySelector('[name="evidence-TOTAL_FORMS"]');
  const max = form.querySelector('[name="evidence-MAX_NUM_FORMS"]');
  if (add && template && rows && total && max) {
    const update = () => { add.hidden = Number(total.value) >= Number(max.value); };
    add.addEventListener('click', () => {
      if (Number(total.value) >= Number(max.value)) return;
      rows.insertAdjacentHTML('beforeend', template.innerHTML.replaceAll('__prefix__', total.value));
      total.value = String(Number(total.value) + 1);
      update();
    });
    update();
  }
})();
