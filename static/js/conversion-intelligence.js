(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-target]");
    if (!button) return;
    var target = document.getElementById(button.getAttribute("data-copy-target"));
    if (!target) return;
    var original = button.innerHTML;
    var copied = function () {
      button.textContent = "تم النسخ";
      window.setTimeout(function () { button.innerHTML = original; }, 1800);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(target.value).then(copied);
      return;
    }
    target.focus();
    target.select();
    if (document.execCommand("copy")) copied();
  });
})();
