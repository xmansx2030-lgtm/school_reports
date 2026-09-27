(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var contactLink = event.target.closest("[data-conversion-whatsapp], [data-conversion-email]");
    if (contactLink) {
      var baseUrl = contactLink.href.split("?")[0];
      var params = new URLSearchParams();
      if (contactLink.hasAttribute("data-conversion-whatsapp")) {
        var whatsappBody = document.getElementById("id_whatsapp_body");
        if (whatsappBody && whatsappBody.value.trim()) params.set("text", whatsappBody.value.trim());
      } else {
        var subject = document.getElementById("id_email_subject");
        var body = document.getElementById("id_email_body");
        if (subject && subject.value.trim()) params.set("subject", subject.value.trim());
        if (body && body.value.trim()) params.set("body", body.value.trim());
      }
      contactLink.href = baseUrl + (params.toString() ? "?" + params.toString() : "");
      return;
    }
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
