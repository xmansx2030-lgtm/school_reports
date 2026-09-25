(function () {
  "use strict";

  const form = document.getElementById("report-form");
  if (!form) return;
  const mode = form.dataset.reportAuthoringMode === "edit" ? "edit" : "create";

  const sections = [
    { key: "goal", toggle: "id_show_goal", field: "id_goal", label: "الهدف" },
    { key: "details", toggle: "id_show_details", field: "id_idea", label: "تفاصيل التقرير" },
    { key: "implementation", toggle: "id_show_implementation", field: "id_implementation_method", label: "آلية التنفيذ" },
    { key: "results", toggle: "id_show_results", field: "id_results", label: "النتائج" },
    { key: "recommendations", toggle: "id_show_recommendations", field: "id_recommendations", label: "التوصيات" },
    { key: "beneficiaries", toggle: "id_show_beneficiaries", field: "id_beneficiaries_count", label: "عدد المستفيدين" },
  ];
  const coreFields = [
    { id: "id_category", group: "category", label: "نوع التقرير", message: "اختر نوع التقرير" },
    { id: "id_title", group: "title", label: "عنوان التقرير", message: "أدخل عنوان التقرير" },
    { id: "id_report_date", group: "report_date", label: "تاريخ التقرير", message: "حدد تاريخ التقرير" },
  ];
  const alertBox = document.getElementById("validationAlert");
  const alertList = document.getElementById("validationAlertList");
  const alertText = document.getElementById("validationAlertText");
  const overlay = document.getElementById("uploadOverlay");
  const uploadDialog = overlay ? overlay.querySelector(".report-upload-dialog") : null;
  const uploadProgress = document.getElementById("uploadProgress");
  const uploadPercent = document.getElementById("upPct");
  const uploadLabel = document.getElementById("upLabel");

  function todayValue() {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const year = today.getFullYear();
    const month = String(today.getMonth() + 1).padStart(2, "0");
    const day = String(today.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function parseDateInput(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return null;
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    date.setHours(0, 0, 0, 0);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function initializeDate() {
    const dateInput = document.getElementById("id_report_date");
    const dayInput = document.getElementById("id_day_name");
    const dayEcho = document.getElementById("reportDayEcho");
    const hijriEcho = document.getElementById("reportDateHijri");
    const futureError = document.getElementById("futureDateErr");
    if (!dateInput) return;

    const days = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"];
    if (mode === "create") {
      dateInput.max = todayValue();
      if (!dateInput.value) dateInput.value = todayValue();
    }
    if (dayInput) {
      dayInput.readOnly = true;
      dayInput.tabIndex = -1;
    }

    function syncDateFields() {
      const value = parseDateInput(dateInput.value);
      const today = parseDateInput(todayValue());
      const isFuture = Boolean(mode === "create" && value && today && value.getTime() > today.getTime());
      if (futureError) futureError.hidden = !isFuture;
      if (isFuture || !value) {
        if (dayInput) dayInput.value = "";
        if (dayEcho) dayEcho.textContent = "";
        if (hijriEcho) hijriEcho.textContent = "";
        return;
      }
      const dayName = days[value.getDay()];
      if (dayInput) dayInput.value = dayName;
      if (dayEcho) dayEcho.textContent = dayName;
      if (hijriEcho) {
        hijriEcho.textContent = window.TawtheeqHijri ? window.TawtheeqHijri.echo(value) : "";
      }
    }

    dateInput.addEventListener("input", syncDateFields);
    dateInput.addEventListener("change", syncDateFields);
    syncDateFields();
  }

  function describeField(input, error) {
    if (!input || !error) return;
    if (!error.id) error.id = `${input.id}-client-error`;
    const describedBy = new Set((input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
    describedBy.add(error.id);
    input.setAttribute("aria-describedby", Array.from(describedBy).join(" "));
  }

  function fieldContainer(config) {
    return config.group
      ? form.querySelector(`.ar-field[data-group="${config.group}"]`)
      : document.getElementById(config.id)?.closest(".ar-field");
  }

  function clientError(config) {
    const container = fieldContainer(config);
    if (!container) return null;
    let error = container.querySelector(".ar-inline-error");
    if (!error) {
      error = document.createElement("p");
      error.className = "ar-error-msg ar-inline-error";
      error.hidden = true;
      container.appendChild(error);
    }
    return error;
  }

  function setValidity(config, valid, message) {
    const input = document.getElementById(config.id);
    const container = fieldContainer(config);
    const error = clientError(config);
    if (container) container.classList.toggle("is-invalid", !valid);
    if (input) {
      input.setAttribute("aria-invalid", valid ? "false" : "true");
      if (error) describeField(input, error);
    }
    if (error) {
      error.textContent = valid ? "" : message;
      error.hidden = valid;
    }
  }

  function syncSection(section) {
    const checkbox = document.getElementById(section.toggle);
    const option = document.querySelector(`[data-section-option="${section.key}"]`);
    const fieldRegion = document.querySelector(`[data-section-field="${section.key}"]`);
    const input = document.getElementById(section.field);
    if (!checkbox) return;
    const selected = checkbox.checked;
    if (option) {
      option.classList.toggle("is-selected", selected);
      option.setAttribute("aria-checked", selected ? "true" : "false");
    }
    if (fieldRegion) fieldRegion.hidden = !selected;
    if (input) {
      input.required = selected;
      input.setAttribute("aria-required", selected ? "true" : "false");
    }
    if (!selected) setValidity({ id: section.field }, true, "");
  }

  function initializeSections() {
    sections.forEach((section) => {
      const checkbox = document.getElementById(section.toggle);
      if (checkbox) checkbox.addEventListener("change", () => syncSection(section));
      syncSection(section);
    });
  }

  function validateField(config) {
    const input = document.getElementById(config.id);
    if (!input) return true;
    const value = String(input.value || "").trim();
    let valid = Boolean(value);
    let message = config.message || `أدخل ${config.label}`;

    if (config.id === "id_report_date" && value) {
      const date = parseDateInput(value);
      const today = parseDateInput(todayValue());
      if (!date) {
        valid = false;
        message = "أدخل تاريخًا صحيحًا";
      } else if (mode === "create" && today && date.getTime() > today.getTime()) {
        valid = false;
        message = "لا يمكن اختيار تاريخ مستقبلي";
      }
    }
    if (config.id === "id_beneficiaries_count" && value) {
      const count = Number(value);
      if (!Number.isFinite(count) || count < 0) {
        valid = false;
        message = "أدخل عددًا صحيحًا غير سالب";
      }
    }
    if (config.id === "id_idea" && input.validity && input.validity.customError) {
      valid = false;
      message = input.validationMessage;
    }
    setValidity(config, valid, message);
    return valid;
  }

  function hideValidationSummary() {
    if (alertBox) alertBox.hidden = true;
  }

  function showValidationSummary(items, description) {
    if (!alertBox || !alertList || !alertText) return;
    alertList.replaceChildren();
    items.forEach((item) => {
      const li = document.createElement("li");
      if (item.id) {
        const link = document.createElement("a");
        link.href = `#${item.id}`;
        link.textContent = item.message;
        link.addEventListener("click", (event) => {
          const target = document.getElementById(item.id);
          if (target) {
            event.preventDefault();
            target.focus();
          }
        });
        li.appendChild(link);
      } else {
        li.textContent = item.message;
      }
      alertList.appendChild(li);
    });
    alertText.textContent = description || "راجع الحقول التالية قبل حفظ التقرير.";
    alertBox.hidden = false;
    alertBox.focus();
  }

  function validateForm() {
    const errors = [];
    coreFields.forEach((config) => {
      if (!validateField(config)) errors.push({ id: config.id, message: config.message });
    });
    const selectedSections = sections.filter((section) => document.getElementById(section.toggle)?.checked);
    if (!selectedSections.length) {
      errors.push({ id: "reportSectionOptions", message: "اختر بندًا واحدًا على الأقل لمحتوى التقرير" });
    }
    selectedSections.forEach((section) => {
      const config = { id: section.field, label: section.label, message: `أدخل ${section.label}` };
      if (!validateField(config)) errors.push({ id: section.field, message: config.message });
    });
    if (errors.length) {
      showValidationSummary(errors);
      return false;
    }
    hideValidationSummary();
    return true;
  }

  function initializeValidation() {
    coreFields.forEach((config) => {
      const input = document.getElementById(config.id);
      if (!input) return;
      input.required = true;
      input.setAttribute("aria-required", "true");
      input.addEventListener(input.tagName === "SELECT" || input.type === "date" ? "change" : "input", () => validateField(config));
    });
    sections.forEach((section) => {
      const input = document.getElementById(section.field);
      if (input) input.addEventListener("input", () => validateField({ id: section.field, label: section.label, message: `أدخل ${section.label}` }));
    });
    document.getElementById("validationAlertClose")?.addEventListener("click", hideValidationSummary);
    form.querySelectorAll(".ar-field").forEach((container) => {
      const input = container.querySelector("input:not([type='hidden']), select, textarea");
      const serverError = container.querySelector(".ar-error-msg:not([hidden])");
      if (!input || !serverError || !serverError.textContent.trim()) return;
      input.setAttribute("aria-invalid", "true");
      describeField(input, serverError);
    });
    document.querySelector("[data-server-error-summary]")?.focus();
  }

  function setSubmitting(submitting) {
    form.setAttribute("aria-busy", submitting ? "true" : "false");
    form.querySelectorAll('button[type="submit"]').forEach((button) => {
      button.disabled = submitting || form.dataset.hasReportTypes === "false";
    });
    if (overlay) {
      overlay.hidden = !submitting;
      overlay.setAttribute("aria-hidden", submitting ? "false" : "true");
    }
    if (submitting && uploadDialog) uploadDialog.focus();
  }

  function setProgress(value, label) {
    const bounded = Math.max(0, Math.min(100, value));
    if (uploadProgress) uploadProgress.value = bounded;
    if (uploadPercent) uploadPercent.textContent = `${bounded}%`;
    if (uploadLabel && label) uploadLabel.textContent = label;
  }

  function submitReport(event) {
    event.preventDefault();
    if (form.dataset.hasReportTypes === "false") {
      document.getElementById("reportTypeSetupNotice")?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (!validateForm()) return;

    setSubmitting(true);
    setProgress(0, "جاري التحضير");
    const request = new XMLHttpRequest();
    request.open("POST", form.action || window.location.href, true);
    request.timeout = 120000;
    request.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    const csrf = form.querySelector('[name="csrfmiddlewaretoken"]');
    if (csrf) request.setRequestHeader("X-CSRFToken", csrf.value);

    request.upload.addEventListener("progress", (progressEvent) => {
      if (!progressEvent.lengthComputable) return;
      const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
      const label = percent < 30 ? "بدء الرفع" : percent < 70 ? "جاري الرفع" : percent < 100 ? "اقترب الاكتمال" : "جاري الحفظ";
      setProgress(percent, label);
    });

    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 400) {
        setProgress(100, "تم الحفظ");
        form.dispatchEvent(new CustomEvent("tawtheeq:submit-success", { bubbles: true, detail: { form } }));
        window.dispatchEvent(new CustomEvent("tawtheeq:task-complete", { detail: { kind: "report" } }));
        window.setTimeout(() => {
          window.location.href = request.responseURL || form.dataset.successUrl;
        }, 300);
        return;
      }
      const contentType = request.getResponseHeader("Content-Type") || "";
      if (contentType.includes("text/html")) {
        document.open();
        document.write(request.responseText);
        document.close();
        return;
      }
      setSubmitting(false);
      showValidationSummary([{ message: "تعذر حفظ التقرير حاليًا. تحقق من البيانات والاتصال ثم أعد المحاولة." }], "لم يتم حفظ التقرير.");
    });

    request.addEventListener("error", () => {
      setSubmitting(false);
      showValidationSummary([{ message: "تعذر الاتصال أثناء الحفظ. أعد المحاولة عند استقرار الشبكة." }], "لم يتم حفظ التقرير.");
    });
    request.addEventListener("timeout", () => {
      setSubmitting(false);
      const retryMessage = mode === "edit"
        ? "استغرق الرفع وقتًا أطول من المتوقع. بقيت مسودة التعديل المحلية متاحة."
        : "استغرق الرفع وقتًا أطول من المتوقع. بقيت المسودة المحلية متاحة.";
      showValidationSummary([{ message: retryMessage }], "لم يكتمل الحفظ بعد.");
    });
    request.send(new FormData(form));
  }

  function initializeLegacyDraftMigration() {
    if (mode !== "create" || !window.localStorage || !window.indexedDB || !form.dataset.legacyDraftKey) return;
    const key = form.dataset.legacyDraftKey;
    let raw = null;
    try {
      raw = window.localStorage.getItem(key);
    } catch (_error) {
      return;
    }
    if (!raw) return;

    const banner = document.getElementById("draftBanner");
    const time = document.getElementById("draftTime");
    try {
      const stored = JSON.parse(raw);
      if (!stored || !stored.d || !banner) return;
      banner.hidden = false;
      if (time && stored.t) time.textContent = `آخر حفظ: ${new Date(stored.t).toLocaleString("ar")}`;
      document.getElementById("draftRestore")?.addEventListener("click", () => {
        Object.entries(stored.d).forEach(([id, value]) => {
          const input = document.getElementById(id);
          if (!input) return;
          if (input.type === "checkbox") input.checked = Boolean(value);
          else input.value = value;
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
        });
        window.localStorage.removeItem(key);
        banner.hidden = true;
      });
      document.getElementById("draftDiscard")?.addEventListener("click", () => {
        window.localStorage.removeItem(key);
        banner.hidden = true;
      });
      form.addEventListener("tawtheeq:submit-success", () => window.localStorage.removeItem(key));
    } catch (_error) {
      window.localStorage.removeItem(key);
    }
  }

  initializeDate();
  initializeSections();
  initializeValidation();
  initializeLegacyDraftMigration();
  form.addEventListener("submit", submitReport);
})();
