(function () {
  "use strict";

  function closeDialog(button) {
    var dialog = button.closest("dialog");
    if (dialog && dialog.open) dialog.close();
  }

  function bindDialogClosers() {
    document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
      button.addEventListener("click", function () {
        closeDialog(button);
      });
    });
  }

  function bindYearDialog() {
    var dialog = document.getElementById("achievementYearDialog");
    var form = document.getElementById("achievementYearForm");
    var select = document.getElementById("achievementYearSelect");
    if (!dialog || !form || !select) return;

    document.querySelectorAll("[data-achievement-year-edit]").forEach(function (button) {
      button.addEventListener("click", function () {
        form.action = button.dataset.url || "";
        select.value = button.dataset.year || "";
        dialog.showModal();
        select.focus();
      });
    });
  }

  function bindImagePreview() {
    var dialog = document.getElementById("achievementImageDialog");
    var preview = document.getElementById("achievementImagePreview");
    if (!dialog || !preview) return;

    document.querySelectorAll("[data-preview-src]").forEach(function (button) {
      button.addEventListener("click", function () {
        preview.src = button.dataset.previewSrc || "";
        preview.alt = button.dataset.previewAlt || "معاينة شاهد";
        dialog.showModal();
      });
    });

    dialog.addEventListener("close", function () {
      preview.removeAttribute("src");
      preview.alt = "";
    });
  }

  function bindReportPicker() {
    var dialog = document.getElementById("achievementReportDialog");
    var search = document.getElementById("achievementReportSearch");
    var results = document.getElementById("achievementReportResults");
    if (!dialog || !search || !results) return;

    var pickerUrl = dialog.dataset.pickerUrl || "";
    var sectionId = "";
    var timer = null;
    var request = null;

    function loadReports(query) {
      if (!pickerUrl || !sectionId) return;
      if (request) request.abort();
      request = new AbortController();
      results.setAttribute("aria-busy", "true");
      results.innerHTML = '<div class="twq-loading"><span class="twq-spinner" aria-hidden="true"></span><p class="twq-loading__message">جارٍ تحميل التقارير المؤهلة...</p></div>';
      var params = new URLSearchParams({ section_id: sectionId, q: query || "" });
      fetch(pickerUrl + "?" + params.toString(), {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: request.signal,
      })
        .then(function (response) {
          if (!response.ok) throw new Error("picker_request_failed");
          return response.text();
        })
        .then(function (html) {
          results.innerHTML = html;
          results.removeAttribute("aria-busy");
        })
        .catch(function (error) {
          if (error.name === "AbortError") return;
          results.innerHTML = '<div class="twq-alert twq-alert--danger" role="alert"><i class="fa-solid fa-circle-exclamation" aria-hidden="true"></i><p>تعذر تحميل التقارير. أغلق النافذة وحاول مرة أخرى.</p></div>';
          results.removeAttribute("aria-busy");
        });
    }

    document.querySelectorAll("[data-open-report-picker]").forEach(function (button) {
      button.addEventListener("click", function () {
        sectionId = button.dataset.sectionId || "";
        search.value = "";
        dialog.showModal();
        loadReports("");
        search.focus();
      });
    });

    search.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        loadReports(search.value.trim());
      }, 220);
    });

    dialog.addEventListener("close", function () {
      if (request) request.abort();
      sectionId = "";
      results.replaceChildren();
      results.removeAttribute("aria-busy");
    });
  }

  function fallbackCopy(input) {
    input.focus();
    input.select();
    input.setSelectionRange(0, input.value.length);
    return document.execCommand("copy");
  }

  function bindCopyLink() {
    var button = document.getElementById("achievementCopyLink");
    var input = document.getElementById("achievementShareUrl");
    var status = document.getElementById("achievementCopyStatus");
    if (!button || !input) return;

    var label = button.querySelector("[data-copy-label]");
    var idleIcon = button.querySelector("[data-copy-idle]");
    var successIcon = button.querySelector("[data-copy-success]");

    button.addEventListener("click", function () {
      var copyPromise;
      if (navigator.clipboard && window.isSecureContext) {
        copyPromise = navigator.clipboard.writeText(input.value);
      } else {
        copyPromise = Promise.resolve(fallbackCopy(input));
      }
      copyPromise
        .then(function () {
          if (label) label.textContent = "تم النسخ";
          if (idleIcon) idleIcon.hidden = true;
          if (successIcon) successIcon.hidden = false;
          if (status) status.textContent = "تم نسخ رابط المشاركة إلى الحافظة.";
          window.setTimeout(function () {
            if (label) label.textContent = "نسخ الرابط";
            if (idleIcon) idleIcon.hidden = false;
            if (successIcon) successIcon.hidden = true;
          }, 2200);
        })
        .catch(function () {
          if (status) status.textContent = "تعذر النسخ التلقائي؛ حدد الرابط وانسخه يدويًا.";
          input.focus();
          input.select();
        });
    });
  }

  function compressImage(file) {
    if (!file.type.startsWith("image/")) return Promise.resolve(file);
    return new Promise(function (resolve) {
      var reader = new FileReader();
      reader.addEventListener("error", function () { resolve(file); });
      reader.addEventListener("load", function () {
        var image = new Image();
        image.addEventListener("error", function () { resolve(file); });
        image.addEventListener("load", function () {
          var width = image.width;
          var height = image.height;
          var maxWidth = 1600;
          var maxHeight = 1600;
          if (width > maxWidth || height > maxHeight) {
            var ratio = Math.min(maxWidth / width, maxHeight / height);
            width = Math.round(width * ratio);
            height = Math.round(height * ratio);
          }
          var canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          var context = canvas.getContext("2d");
          if (!context) {
            resolve(file);
            return;
          }
          context.drawImage(image, 0, 0, width, height);
          canvas.toBlob(function (blob) {
            if (!blob) {
              resolve(file);
              return;
            }
            resolve(new File([blob], file.name, { type: "image/jpeg", lastModified: file.lastModified }));
          }, "image/jpeg", 0.72);
        });
        image.src = String(reader.result || "");
      });
      reader.readAsDataURL(file);
    });
  }

  function bindEvidenceCompression() {
    document.querySelectorAll("[data-achievement-upload]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (form.dataset.processed === "true") return;
        var input = form.querySelector('input[type="file"]');
        if (!input || !input.files || !input.files.length) return;
        event.preventDefault();
        var button = form.querySelector('button[type="submit"]');
        var label = form.querySelector("[data-upload-label]");
        if (button) button.disabled = true;
        if (label) label.textContent = "جارٍ تجهيز الصور...";

        Promise.all(Array.from(input.files).map(compressImage))
          .then(function (files) {
            var transfer = new DataTransfer();
            files.forEach(function (file) { transfer.items.add(file); });
            input.files = transfer.files;
          })
          .finally(function () {
            form.dataset.processed = "true";
            HTMLFormElement.prototype.submit.call(form);
          });
      });
    });
  }

  function focusErrorSummary() {
    var summary = document.querySelector(".achievement-error-summary");
    if (summary) summary.focus();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.documentElement.dataset.achievementReady = "true";
    bindDialogClosers();
    bindYearDialog();
    bindImagePreview();
    bindReportPicker();
    bindCopyLink();
    bindEvidenceCompression();
    focusErrorSummary();
  });
})();
