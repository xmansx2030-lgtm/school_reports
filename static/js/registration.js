(function () {
  "use strict";

  const registrationForm = document.querySelector("[data-registration-form]");
  const errorSummary = document.querySelector("[data-registration-error-summary]");

  if (errorSummary) {
    window.requestAnimationFrame(function () {
      errorSummary.focus();
    });
  }

  if (registrationForm) {
    registrationForm.addEventListener("submit", function () {
      if (!registrationForm.checkValidity()) return;
      const submitButton = registrationForm.querySelector("[type='submit']");
      const submitStatus = document.getElementById("registrationSubmitStatus");
      if (submitButton) {
        submitButton.setAttribute("aria-busy", "true");
        submitButton.disabled = true;
      }
      if (submitStatus) submitStatus.textContent = "جارٍ إنشاء المدرسة والحساب والتجربة بأمان…";
    });
  }

  const toast = document.getElementById("toast");
  const copyAll = document.getElementById("copyCredentials");
  if (!toast || !copyAll) return;

  let toastTimer;
  let receiptPreserved = false;
  let pendingDashboardUrl = "";
  let lastFocusedElement = null;
  const copiedCredentialTargets = { loginPhone: false, loginPassword: false };
  const leaveConfirmation = document.getElementById("leaveConfirmation");
  const leaveCard = leaveConfirmation ? leaveConfirmation.querySelector(".registration-leave__card") : null;
  const leaveCancel = leaveConfirmation ? leaveConfirmation.querySelector("[data-leave-cancel]") : null;
  const leaveConfirm = leaveConfirmation ? leaveConfirmation.querySelector("[data-leave-confirm]") : null;
  const launchDashboard = document.getElementById("launchDashboard");

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toast.classList.remove("is-visible");
    }, 2400);
  }

  function legacyCopy(value) {
    const temporary = document.createElement("textarea");
    temporary.value = value;
    temporary.readOnly = true;
    temporary.setAttribute("aria-hidden", "true");
    temporary.className = "registration-copy-fallback";
    document.body.appendChild(temporary);
    temporary.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (error) {
      copied = false;
    }
    temporary.remove();
    return copied;
  }

  function copyText(value, message, onCopied) {
    function copied() {
      showToast(message);
      if (onCopied) onCopied();
    }

    function failed() {
      showToast("تعذّر النسخ تلقائيًا؛ حدّد البيانات وانسخها يدويًا.");
    }

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(value).then(copied).catch(function () {
        if (legacyCopy(value)) copied();
        else failed();
      });
      return;
    }

    if (legacyCopy(value)) copied();
    else failed();
  }

  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      const targetId = button.getAttribute("data-copy-target");
      const input = targetId ? document.getElementById(targetId) : null;
      if (!input) return;
      copyText(input.value, "تم النسخ", function () {
        if (Object.prototype.hasOwnProperty.call(copiedCredentialTargets, targetId)) {
          copiedCredentialTargets[targetId] = true;
          receiptPreserved = copiedCredentialTargets.loginPhone && copiedCredentialTargets.loginPassword;
        }
      });
    });
  });

  copyAll.addEventListener("click", function () {
    const phone = document.getElementById("loginPhone");
    const password = document.getElementById("loginPassword");
    const loginPath = copyAll.getAttribute("data-login-url") || "/login/";
    if (!phone || !password) return;
    const loginUrl = new URL(loginPath, window.location.origin).href;
    copyText(
      "منصة توثيق\nرابط الدخول: " + loginUrl + "\nرقم الجوال: " + phone.value + "\nكلمة المرور: " + password.value,
      "تم نسخ بيانات الدخول",
      function () {
        receiptPreserved = true;
      }
    );
  });

  const printReceipt = document.getElementById("printReceipt");
  if (printReceipt) {
    printReceipt.addEventListener("click", function () {
      receiptPreserved = true;
      window.print();
    });
  }

  function closeLeaveConfirmation() {
    if (!leaveConfirmation) return;
    leaveConfirmation.hidden = true;
    if (lastFocusedElement) lastFocusedElement.focus();
  }

  if (launchDashboard && leaveConfirmation && leaveCancel && leaveConfirm) {
    launchDashboard.addEventListener("click", function (event) {
      if (receiptPreserved) return;
      event.preventDefault();
      lastFocusedElement = event.currentTarget;
      pendingDashboardUrl = event.currentTarget.href;
      leaveConfirmation.hidden = false;
      leaveCancel.focus();
    });

    leaveCancel.addEventListener("click", closeLeaveConfirmation);
    leaveConfirm.addEventListener("click", function () {
      if (pendingDashboardUrl) window.location.assign(pendingDashboardUrl);
    });

    leaveConfirmation.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeLeaveConfirmation();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(leaveConfirmation.querySelectorAll("button:not([disabled]), a[href]"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    leaveConfirmation.addEventListener("click", function (event) {
      if (event.target === leaveConfirmation) closeLeaveConfirmation();
    });

    if (leaveCard) leaveCard.setAttribute("aria-live", "polite");
  }

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) window.location.reload();
  });
})();
