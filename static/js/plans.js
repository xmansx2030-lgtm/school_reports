(function () {
  "use strict";

  function copyText(value, onSuccess, onFailure) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(onSuccess, onFailure);
      return;
    }

    try {
      if (document.execCommand("copy")) {
        onSuccess();
        return;
      }
    } catch (error) {
      // The selected value remains available for manual copying.
    }
    onFailure();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-plan-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        var form = button.closest("form");
        var field = form && form.querySelector("[data-plan-action-field]");
        if (field) field.value = button.getAttribute("data-plan-action") || "";
      });
    });

    document.querySelectorAll("[data-initiative-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        var form = button.closest("form");
        var field = form && form.querySelector("[data-initiative-action-field]");
        if (field) field.value = button.getAttribute("data-initiative-action") || "";
      });
    });

    var copyButton = document.querySelector("[data-plan-copy]");
    var urlField = document.querySelector("[data-plan-url]");
    if (copyButton && urlField) {
      copyButton.addEventListener("click", function () {
        urlField.select();
        copyText(
          urlField.value,
          function () {
            copyButton.textContent = "نُسخ الرابط";
            copyButton.setAttribute("data-copy-state", "success");
          },
          function () {
            copyButton.textContent = "حدّد الرابط وانسخه";
            copyButton.setAttribute("data-copy-state", "error");
          }
        );
      });
    }

    var shareButton = document.querySelector("[data-plan-share]");
    if (shareButton) {
      shareButton.addEventListener("click", function () {
        var url = shareButton.getAttribute("data-url") || "";
        var title = shareButton.getAttribute("data-title") || "";
        if (navigator.share) {
          navigator.share({ title: title, text: title, url: url }).catch(function () {});
          return;
        }
        copyText(
          url,
          function () { shareButton.textContent = "نُسخ الرابط"; },
          function () { shareButton.textContent = "تعذّر النسخ"; }
        );
      });
    }

    var errorSummary = document.querySelector("[data-plan-error-summary]");
    if (errorSummary) errorSummary.focus();

    document.querySelectorAll(".twq-field--error").forEach(function (field) {
      var control = field.querySelector("input, select, textarea");
      var message = field.querySelector(".twq-field__message[id]");
      if (!control) return;
      control.setAttribute("aria-invalid", "true");
      if (message) control.setAttribute("aria-describedby", message.id);
    });
  });
}());
