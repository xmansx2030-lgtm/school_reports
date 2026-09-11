(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;
  if (!body) return;

  function isStandalone() {
    return Boolean(
      (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
      window.navigator.standalone
    );
  }

  function syncDisplayMode() {
    var standalone = isStandalone();
    root.classList.toggle("is-standalone", standalone);
    body.dataset.displayMode = standalone ? "standalone" : "browser";
  }

  function enhanceNavigation() {
    document.querySelectorAll(".tab.is-active, .np-item.is-active, .bottom-nav .is-active").forEach(function (item) {
      if (item.matches("a")) item.setAttribute("aria-current", "page");
    });
  }

  function enhanceEmptyStates() {
    document.querySelectorAll(".empty-state, .royal-empty, .no-data, [data-empty-state]").forEach(function (state) {
      if (!state.hasAttribute("role")) state.setAttribute("role", "status");
    });
  }

  function enhanceValidation() {
    document.querySelectorAll("input:required, select:required, textarea:required").forEach(function (field) {
      field.setAttribute("aria-required", "true");
    });

    document.querySelectorAll(".errorlist, .field-error, .invalid-feedback, .form-error").forEach(function (error, index) {
      var holder = error.closest(".form-group, .field-group, .form-field, .ss-group") || error.parentElement;
      if (!holder) return;
      var field = holder.querySelector("input, select, textarea");
      if (!field) return;
      if (!error.id) error.id = "field-error-" + index;
      field.setAttribute("aria-invalid", "true");
      var describedBy = (field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      if (describedBy.indexOf(error.id) === -1) describedBy.push(error.id);
      field.setAttribute("aria-describedby", describedBy.join(" "));
    });
  }

  function enhanceScrollableTables() {
    document.querySelectorAll(
      ".table-wrap, .table-responsive, .royal-table-wrap, [class*='table-wrap'], [class*='table-responsive']"
    ).forEach(function (region) {
      if (!region.querySelector("table")) return;
      if (!region.hasAttribute("tabindex")) region.tabIndex = 0;
      if (!region.hasAttribute("role")) region.setAttribute("role", "region");
      if (!region.hasAttribute("aria-label")) {
        var caption = region.querySelector("caption");
        region.setAttribute("aria-label", caption ? caption.textContent.trim() : "جدول بيانات قابل للتمرير");
      }
      region.classList.toggle("is-scrollable", region.scrollWidth > region.clientWidth + 2);
    });
  }

  function bindSafePageActions() {
    document.querySelectorAll("[data-history-back]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (window.history.length > 1) window.history.back();
        else window.location.assign("/");
      });
    });
    document.querySelectorAll("[data-page-reload]").forEach(function (button) {
      button.addEventListener("click", function () { window.location.reload(); });
    });
  }

  function bindActionMenus() {
    document.addEventListener("click", function (event) {
      var current = event.target.closest(".ui-actions-menu");
      document.querySelectorAll(".ui-actions-menu[open]").forEach(function (menu) {
        if (menu !== current) menu.removeAttribute("open");
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      var openMenu = document.querySelector(".ui-actions-menu[open]");
      if (!openMenu) return;
      openMenu.removeAttribute("open");
      var summary = openMenu.querySelector("summary");
      if (summary) summary.focus();
    });
  }

  function setSubmitting(form, submitter) {
    if (!submitter || submitter.matches("[data-no-loading]")) return;
    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    submitter.classList.add("is-loading");
    submitter.setAttribute("aria-disabled", "true");
    if (!submitter.querySelector(".ui-loading-indicator")) {
      var indicator = document.createElement("span");
      indicator.className = "ui-loading-indicator";
      indicator.setAttribute("aria-hidden", "true");
      submitter.prepend(indicator);
    }
  }

  function resetSubmitting() {
    document.querySelectorAll("form[data-submitting='true']").forEach(function (form) {
      form.removeAttribute("data-submitting");
      form.removeAttribute("aria-busy");
      form.querySelectorAll(".is-loading").forEach(function (button) {
        button.classList.remove("is-loading");
        button.removeAttribute("aria-disabled");
        var indicator = button.querySelector(".ui-loading-indicator");
        if (indicator) indicator.remove();
      });
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || form.matches("[data-allow-repeat-submit]")) return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    var submitter = event.submitter || form.querySelector("button[type='submit'], input[type='submit']");
    window.setTimeout(function () {
      if (!event.defaultPrevented) setSubmitting(form, submitter);
    }, 0);
  });

  window.addEventListener("pageshow", resetSubmitting);
  window.addEventListener("resize", enhanceScrollableTables, { passive: true });
  if (window.matchMedia) {
    var displayMode = window.matchMedia("(display-mode: standalone)");
    if (displayMode.addEventListener) displayMode.addEventListener("change", syncDisplayMode);
  }

  syncDisplayMode();
  enhanceNavigation();
  enhanceEmptyStates();
  enhanceValidation();
  enhanceScrollableTables();
  bindSafePageActions();
  bindActionMenus();
  root.classList.add("ui-ready");
}());
