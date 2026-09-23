(function () {
  "use strict";

  function byId(id) {
    return id ? document.getElementById(id) : null;
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function initCreate() {
    var form = document.querySelector("[data-assignment-create]");
    if (!form) return;

    var summary = byId("assignmentErrorSummary");
    if (summary) summary.focus();

    var due = byId(form.getAttribute("data-due-input"));
    var echo = byId("assignmentDueHijri");
    function paintEcho() {
      if (!echo || !due) return;
      echo.textContent = window.TawtheeqHijri
        ? window.TawtheeqHijri.echo(due.value, "الموافق ")
        : "";
    }

    if (due) {
      due.addEventListener("change", paintEcho);
      due.addEventListener("input", paintEcho);
      paintEcho();
    }

    document.querySelectorAll("[data-assignment-days]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!due) return;
        var when = new Date();
        when.setDate(
          when.getDate() + parseInt(button.getAttribute("data-assignment-days"), 10)
        );
        due.value =
          when.getFullYear() +
          "-" +
          pad(when.getMonth() + 1) +
          "-" +
          pad(when.getDate()) +
          "T" +
          pad(when.getHours()) +
          ":" +
          pad(when.getMinutes());
        paintEcho();
      });
    });

    var requiresEvidence = byId(form.getAttribute("data-evidence-input"));
    var evidenceCount = byId("assignmentEvidenceCount");
    function paintEvidence() {
      if (!requiresEvidence || !evidenceCount) return;
      evidenceCount.hidden = !requiresEvidence.checked;
    }
    if (requiresEvidence) {
      requiresEvidence.addEventListener("change", paintEvidence);
      paintEvidence();
    }

    var grid = byId("assignmentTargetGrid");
    if (!grid) return;

    var targets = Array.prototype.slice.call(
      grid.querySelectorAll("[data-assignment-target]")
    );
    var search = byId("assignmentTargetSearch");
    var filters = Array.prototype.slice.call(
      document.querySelectorAll("[data-assignment-role]")
    );
    var counter = byId("assignmentTargetCount");
    var noResults = byId("assignmentTargetNoResults");
    var activeRole = "all";

    function visible(target) {
      return !target.hidden;
    }

    function paintCount() {
      var selected = 0;
      targets.forEach(function (target) {
        var checkbox = target.querySelector('input[type="checkbox"]');
        var isSelected = Boolean(checkbox && checkbox.checked);
        target.classList.toggle("is-selected", isSelected);
        if (isSelected) selected += 1;
      });
      if (counter) counter.textContent = selected;
    }

    function paintTargets() {
      var term = ((search && search.value) || "").trim().toLowerCase();
      var shown = 0;
      targets.forEach(function (target) {
        var matchesRole =
          activeRole === "all" ||
          target.getAttribute("data-assignment-target-role") === activeRole;
        var matchesTerm =
          !term ||
          (target.getAttribute("data-assignment-target-search") || "")
            .toLowerCase()
            .indexOf(term) !== -1;
        target.hidden = !(matchesRole && matchesTerm);
        if (!target.hidden) shown += 1;
      });
      if (noResults) noResults.hidden = shown !== 0;
    }

    if (search) search.addEventListener("input", paintTargets);
    filters.forEach(function (filter) {
      filter.addEventListener("click", function () {
        activeRole = filter.getAttribute("data-assignment-role") || "all";
        filters.forEach(function (other) {
          other.setAttribute("aria-pressed", other === filter ? "true" : "false");
        });
        paintTargets();
      });
    });

    var selectVisible = byId("assignmentSelectVisible");
    if (selectVisible) {
      selectVisible.addEventListener("click", function () {
        targets.filter(visible).forEach(function (target) {
          var checkbox = target.querySelector('input[type="checkbox"]');
          if (checkbox) checkbox.checked = true;
        });
        paintCount();
      });
    }

    var clearSelection = byId("assignmentClearSelection");
    if (clearSelection) {
      clearSelection.addEventListener("click", function () {
        targets.forEach(function (target) {
          var checkbox = target.querySelector('input[type="checkbox"]');
          if (checkbox) checkbox.checked = false;
        });
        paintCount();
      });
    }

    grid.addEventListener("change", paintCount);
    paintCount();

    form.addEventListener("submit", function (event) {
      if (form.getAttribute("data-has-assignees") === "false") {
        event.preventDefault();
        var setupLink = byId("assignmentTeamSetupLink");
        var setupNotice = byId("assignmentTeamSetupNotice");
        var setupTarget = setupLink || setupNotice;
        if (setupTarget) {
          setupTarget.scrollIntoView({ behavior: "smooth", block: "center" });
          if (setupLink) setupLink.focus({ preventScroll: true });
        }
        return;
      }

      var selected = targets.some(function (target) {
        var checkbox = target.querySelector('input[type="checkbox"]');
        return checkbox && checkbox.checked;
      });
      if (selected) return;

      event.preventDefault();
      activeRole = "all";
      filters.forEach(function (filter) {
        filter.setAttribute(
          "aria-pressed",
          filter.getAttribute("data-assignment-role") === "all" ? "true" : "false"
        );
      });
      if (search) search.value = "";
      paintTargets();
      grid.scrollIntoView({ behavior: "smooth", block: "center" });
      if (search) search.focus();
    });
  }

  function copyText(value, done) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, function () {});
      return;
    }

    var field = byId("assignmentShareUrl");
    if (!field) return;
    field.select();
    try {
      if (document.execCommand("copy")) done();
    } catch (error) {
      /* The selected URL remains available for manual copying. */
    }
  }

  function initShare() {
    var field = byId("assignmentShareUrl");
    var copyButton = byId("assignmentCopyLink");
    if (field && copyButton) {
      copyButton.addEventListener("click", function () {
        field.select();
        copyText(field.value, function () {
          copyButton.querySelector("span").textContent = "نُسخ الرابط";
        });
      });
    }

    var shareButton = byId("assignmentShare");
    if (!shareButton) return;
    shareButton.addEventListener("click", function () {
      var url = shareButton.getAttribute("data-url") || "";
      var title = shareButton.getAttribute("data-title") || "";
      if (navigator.share) {
        navigator.share({ title: title, text: title, url: url }).catch(function () {});
        return;
      }
      copyText(url, function () {
        shareButton.querySelector("span").textContent = "نُسخ الرابط";
      });
    });
  }

  function initApprovalActions() {
    var field = byId("assignmentApprovalAction");
    if (!field) return;
    document.querySelectorAll("[data-assignment-approval]").forEach(function (button) {
      button.addEventListener("click", function () {
        field.value = button.getAttribute("data-assignment-approval") || "";
      });
    });
  }

  initCreate();
  initShare();
  initApprovalActions();
})();
