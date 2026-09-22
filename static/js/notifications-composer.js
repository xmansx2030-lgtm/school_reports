(function () {
  "use strict";

  const root = document.querySelector(".notifications-composer");
  const form = document.getElementById("notifyForm");
  if (!root || !form) return;

  const byId = (id) => document.getElementById(id);
  const errorSummary = byId("notificationErrorSummary");
  const titleInput = byId("id_title");
  const messageInput = byId("id_message");
  const importantInput = byId("id_is_important");
  const expiresInput = byId("id_expires_at");
  const signatureInput = byId("id_requires_signature");
  const signatureAckInput = byId("id_signature_ack_text");
  const attachmentInput = byId("id_attachment");
  const communicationInputs = Array.from(form.querySelectorAll('input[name="communication_type"]'));

  function focusServerErrors() {
    const target = errorSummary || form.querySelector('[aria-invalid="true"]');
    if (target && typeof target.focus === "function") target.focus();
  }

  if (errorSummary) {
    errorSummary.addEventListener("click", (event) => {
      const link = event.target.closest('a[href^="#"]');
      if (!link) return;
      const field = document.getElementById(decodeURIComponent(link.hash.slice(1)));
      if (!field || typeof field.focus !== "function") return;
      event.preventDefault();
      field.focus();
    });
  }

  function selectedKind() {
    const selected = communicationInputs.find((input) => input.checked);
    return selected ? selected.value : "notification";
  }

  function setHidden(element, hidden) {
    if (element) element.hidden = Boolean(hidden);
  }

  function updatePreview() {
    const isNewsletter = selectedKind() === "newsletter";
    const previewTitle = byId("pv_title");
    const previewBody = byId("pv_body");
    if (previewTitle) {
      previewTitle.textContent = (titleInput && titleInput.value.trim()) || (isNewsletter ? "عنوان النشرة" : "عنوان الإشعار");
    }
    if (previewBody) {
      previewBody.textContent = (messageInput && messageInput.value.trim()) || (isNewsletter ? "ستظهر مقدمة النشرة هنا." : "سيظهر نص الإشعار هنا.");
    }
    setHidden(byId("pv_important"), !(importantInput && importantInput.checked));
    setHidden(byId("pv_signature"), !(isNewsletter && signatureInput && signatureInput.checked));
    setHidden(byId("pv_expires"), !(expiresInput && expiresInput.value.trim()));
    setHidden(byId("pv_newsletter"), !isNewsletter);
  }

  function syncKind() {
    const isNewsletter = selectedKind() === "newsletter";
    setHidden(byId("newsletterOptions"), !isNewsletter);
    setHidden(byId("signatureOptions"), !(isNewsletter && signatureInput && signatureInput.checked));
    if (titleInput) {
      titleInput.required = isNewsletter;
      titleInput.placeholder = isNewsletter ? "عنوان النشرة" : "عنوان الإشعار (اختياري)";
    }
    const titleLabel = byId("titleFieldLabel");
    const titleMark = byId("titleRequiredMark");
    const messageLabel = byId("messageFieldLabel");
    const submitLabel = byId("submitLabel");
    const submitButton = byId("notificationSubmit");
    if (titleLabel) titleLabel.textContent = isNewsletter ? "عنوان النشرة" : "عنوان (اختياري)";
    setHidden(titleMark, !isNewsletter);
    if (messageLabel) messageLabel.firstChild.textContent = isNewsletter ? "مقدمة النشرة " : "نص الإشعار ";
    if (messageInput) messageInput.placeholder = isNewsletter ? "اكتب مقدمة مختصرة توضّح محتوى النشرة…" : "اكتب نص الإشعار…";
    if (submitLabel) {
      submitLabel.textContent = isNewsletter
        ? (submitButton && submitButton.dataset.newsletterLabel) || "إرسال النشرة"
        : (submitButton && submitButton.dataset.notificationLabel) || "إرسال الإشعار";
    }
    if (signatureAckInput && isNewsletter) {
      const circularDefault = "أقرّ بأنني اطلعت على هذا التعميم وفهمت ما ورد فيه وأتعهد بالالتزام به.";
      if (!signatureAckInput.value.trim() || signatureAckInput.value.trim() === circularDefault) {
        signatureAckInput.value = "أقرّ بأنني اطلعت على هذه النشرة وفهمت ما ورد فيها.";
      }
    }
    updatePreview();
  }

  communicationInputs.forEach((input) => input.addEventListener("change", syncKind));
  [titleInput, messageInput, importantInput, expiresInput].forEach((input) => {
    if (!input) return;
    input.addEventListener("input", updatePreview);
    input.addEventListener("change", updatePreview);
  });
  if (signatureInput) signatureInput.addEventListener("change", syncKind);
  if (attachmentInput) {
    attachmentInput.addEventListener("change", () => {
      const output = byId("notificationAttachmentName");
      if (output) output.textContent = attachmentInput.files && attachmentInput.files[0] ? attachmentInput.files[0].name : "لم يُحدد ملف.";
    });
  }

  const teacherList = byId("teacherList");
  const departmentList = byId("departmentList");
  const teacherSearch = byId("teacherSearch");
  const scopeSelect = byId("id_audience_scope");
  const schoolSelect = byId("id_target_school");

  function checkboxes(container, name) {
    if (!container) return [];
    return Array.from(container.querySelectorAll(`input[type="checkbox"][name="${name}"]`));
  }

  function selectedLabels(container, name) {
    return checkboxes(container, name).filter((input) => input.checked).map((input) => {
      const label = input.closest("label");
      return label ? label.textContent.replace(/\s+/g, " ").trim() : "";
    }).filter(Boolean);
  }

  function renderChips(target, labels, overflowLabel) {
    if (!target) return;
    target.replaceChildren();
    labels.slice(0, 8).forEach((label) => {
      const chip = document.createElement("span");
      chip.className = "notifications-selected-chip";
      chip.textContent = label;
      target.appendChild(chip);
    });
    if (labels.length > 8) {
      const chip = document.createElement("span");
      chip.className = "notifications-selected-chip";
      chip.textContent = `+${labels.length - 8} ${overflowLabel}`;
      target.appendChild(chip);
    }
  }

  function syncRecipients() {
    const people = selectedLabels(teacherList, "teachers");
    const departments = selectedLabels(departmentList, "target_department");
    renderChips(byId("selectedTeachersPreview"), people, "آخرون");
    renderChips(byId("selectedDepartmentsPreview"), departments, "أقسام أخرى");
    const summary = byId("recipientSummaryText");
    if (summary) {
      const parts = [];
      if (departments.length) parts.push(`${departments.length} ${departments.length === 1 ? "قسم" : "أقسام"}`);
      if (people.length) parts.push(`${people.length} ${people.length === 1 ? "فرد" : "أفراد"}`);
      summary.textContent = parts.length ? `${parts.join(" + ")}؛ تُزال الاختيارات المكررة تلقائيًا.` : "لم يتم تحديد مستلمين بعد.";
    }
  }

  function filterTeachers() {
    const query = (teacherSearch ? teacherSearch.value : "").trim().toLocaleLowerCase("ar");
    checkboxes(teacherList, "teachers").forEach((input) => {
      const row = input.closest("li") || input.closest("label");
      if (row) row.hidden = Boolean(query && !row.textContent.toLocaleLowerCase("ar").includes(query));
    });
  }

  function toggleVisibleTeachers(checked) {
    checkboxes(teacherList, "teachers").forEach((input) => {
      const row = input.closest("li") || input.closest("label");
      if (!row || !row.hidden) input.checked = checked;
    });
    syncRecipients();
  }

  function replaceChecklist(container, results, fieldName, idPrefix, emptyText) {
    if (!container) return;
    if (!results.length) {
      const empty = document.createElement("p");
      empty.className = "notifications-help";
      empty.textContent = emptyText;
      container.replaceChildren(empty);
      return;
    }
    const list = document.createElement("ul");
    results.forEach((item, index) => {
      const row = document.createElement("li");
      const label = document.createElement("label");
      const input = document.createElement("input");
      const text = document.createElement("span");
      input.type = "checkbox";
      input.name = fieldName;
      input.value = String(item.id);
      input.id = `${idPrefix}_${index}`;
      label.htmlFor = input.id;
      text.textContent = item.name || "—";
      label.append(input, text);
      row.appendChild(label);
      list.appendChild(row);
    });
    container.replaceChildren(list);
  }

  async function refreshList(url, container, fieldName, idPrefix, emptyText) {
    if (!url || !container) return;
    const selected = new Set(checkboxes(container, fieldName).filter((input) => input.checked).map((input) => input.value));
    container.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
      if (!response.ok) throw new Error("request_failed");
      const payload = await response.json();
      const results = Array.isArray(payload.results) ? payload.results : [];
      replaceChecklist(container, results, fieldName, idPrefix, emptyText);
      checkboxes(container, fieldName).forEach((input) => { input.checked = selected.has(input.value); });
    } catch (_error) {
      const status = document.createElement("p");
      status.className = "notifications-field-error";
      status.textContent = "تعذّر تحديث القائمة. بقيت الاختيارات السابقة محفوظة؛ أعد المحاولة.";
      container.appendChild(status);
    } finally {
      container.removeAttribute("aria-busy");
      filterTeachers();
      syncRecipients();
    }
  }

  async function refreshAudience(clearDepartments) {
    const scope = scopeSelect ? scopeSelect.value : "";
    const school = schoolSelect ? schoolSelect.value : "";
    if (clearDepartments) checkboxes(departmentList, "target_department").forEach((input) => { input.checked = false; });
    const teacherParams = new URLSearchParams();
    if (scope) teacherParams.set("audience_scope", scope);
    if (school) teacherParams.set("target_school", school);
    const departmentParams = new URLSearchParams();
    if (scope !== "all" && school) departmentParams.set("school", school);
    const teachersUrl = root.dataset.teachersUrl ? `${root.dataset.teachersUrl}?${teacherParams}` : "";
    const departmentsUrl = root.dataset.departmentsUrl ? `${root.dataset.departmentsUrl}?${departmentParams}` : "";
    await Promise.all([
      refreshList(teachersUrl, teacherList, "teachers", "id_teachers", "لا يوجد مستلمون في النطاق المحدد."),
      refreshList(departmentsUrl, departmentList, "target_department", "id_target_department", "لا توجد أقسام متاحة في النطاق المحدد."),
    ]);
  }

  if (teacherSearch) teacherSearch.addEventListener("input", filterTeachers);
  if (teacherList) teacherList.addEventListener("change", syncRecipients);
  if (departmentList) departmentList.addEventListener("change", syncRecipients);
  const selectAll = byId("selectAllTeachers");
  const clearAll = byId("clearAllTeachers");
  const clearDepartments = byId("clearAllDepartments");
  if (selectAll) selectAll.addEventListener("click", () => toggleVisibleTeachers(true));
  if (clearAll) clearAll.addEventListener("click", () => toggleVisibleTeachers(false));
  if (clearDepartments) clearDepartments.addEventListener("click", () => {
    checkboxes(departmentList, "target_department").forEach((input) => { input.checked = false; });
    syncRecipients();
  });
  if (scopeSelect) scopeSelect.addEventListener("change", () => refreshAudience(true));
  if (schoolSelect) schoolSelect.addEventListener("change", () => refreshAudience(true));

  form.addEventListener("submit", (event) => {
    if (form.dataset.hasRecipients === "false") {
      event.preventDefault();
      const target = byId("notificationTeamSetupLink") || byId("notificationTeamSetupNotice");
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        if (typeof target.focus === "function") target.focus({ preventScroll: true });
      }
      return;
    }
    form.querySelectorAll('button[type="submit"]').forEach((button) => {
      button.disabled = true;
      button.setAttribute("aria-disabled", "true");
    });
    const label = byId("submitLabel");
    if (label) label.textContent = "جارٍ الإرسال…";
  });

  syncKind();
  filterTeachers();
  syncRecipients();
  focusServerErrors();
})();
