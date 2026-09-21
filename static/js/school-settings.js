(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("schoolSettingsForm");
    var button = document.getElementById("submitBtn");
    var buttonLabel = document.getElementById("submitBtnLabel");
    var saveStatus = document.getElementById("saveStatus");
    if (!form || !button || !buttonLabel) return;

    var defaultLabel = buttonLabel.textContent;
    function restoreSubmitButton() {
      button.disabled = false;
      buttonLabel.textContent = defaultLabel;
      form.setAttribute("aria-busy", "false");
      if (saveStatus) saveStatus.textContent = "";
    }

    form.addEventListener("submit", function () {
      button.disabled = true;
      buttonLabel.textContent = "جارٍ الحفظ...";
      form.setAttribute("aria-busy", "true");
      if (saveStatus) saveStatus.textContent = "جارٍ حفظ تغييرات إعدادات المدرسة.";
    });

    window.addEventListener("pageshow", function (event) {
      if (event.persisted) restoreSubmitButton();
    });
  });
}());
