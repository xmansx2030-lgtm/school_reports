(function () {
  "use strict";

  document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
    const inputId = button.getAttribute("data-password-toggle");
    const input = inputId ? document.getElementById(inputId) : null;
    if (!input) return;

    button.addEventListener("click", function () {
      const shouldShow = input.type === "password";
      const icon = button.querySelector("i");
      input.type = shouldShow ? "text" : "password";
      button.setAttribute("aria-pressed", shouldShow ? "true" : "false");
      button.setAttribute("aria-label", shouldShow ? "إخفاء كلمة المرور" : "إظهار كلمة المرور");
      button.title = button.getAttribute("aria-label");
      if (icon) {
        icon.classList.toggle("fa-eye", shouldShow);
        icon.classList.toggle("fa-eye-slash", !shouldShow);
      }
    });
  });
})();
