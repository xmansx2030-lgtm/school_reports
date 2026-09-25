(() => {
  const form = document.querySelector('.personal-report-form');
  if (!form) return;
  for (const section of form.querySelectorAll('[data-personal-section]')) {
    const control = form.querySelector(`[name="${section.dataset.personalSection}"]`);
    if (!control) continue;
    const update = () => { section.hidden = !control.checked; };
    control.addEventListener('change', update);
    update();
  }
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
