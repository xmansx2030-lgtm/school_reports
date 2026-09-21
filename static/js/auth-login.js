(function () {
  "use strict";

  const toggleBtn = document.getElementById("togglePass");
  const passInput = document.getElementById("password");

  if (toggleBtn && passInput) {
    toggleBtn.addEventListener("click", function () {
      const shouldShow = passInput.type === "password";
      const icon = toggleBtn.querySelector("i");
      passInput.type = shouldShow ? "text" : "password";
      toggleBtn.setAttribute("aria-pressed", shouldShow ? "true" : "false");
      toggleBtn.setAttribute("aria-label", shouldShow ? "إخفاء كلمة المرور" : "إظهار كلمة المرور");
      toggleBtn.title = toggleBtn.getAttribute("aria-label");
      if (icon) {
        icon.classList.toggle("fa-eye", shouldShow);
        icon.classList.toggle("fa-eye-slash", !shouldShow);
      }
    });
  }

  const loginForm = document.getElementById("loginForm");
  const loginButton = document.getElementById("loginSubmitBtn");
  if (loginForm && loginButton) {
    let submitted = false;
    loginForm.addEventListener("submit", function (event) {
      if (submitted) {
        event.preventDefault();
        return;
      }
      submitted = true;
      loginButton.disabled = true;
      loginButton.setAttribute("aria-disabled", "true");
      const label = loginButton.querySelector("span");
      const icon = loginButton.querySelector("i");
      if (label) label.textContent = "جارٍ تسجيل الدخول...";
      if (icon) icon.className = "fa-solid fa-spinner fa-spin";
    });
  }

  const passkeyBtn = document.getElementById("passkeyLoginBtn");
  const statusBox = document.getElementById("passkeyStatus");
  const divider = document.getElementById("passkeyDivider");
  const panel = document.getElementById("passkeyPanel");
  const labelSpan = passkeyBtn ? passkeyBtn.querySelector("[data-passkey-label]") : null;
  const identifier = document.getElementById("identifier");
  const nextInput = document.querySelector('input[name="next"]');
  const csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');

  if (!passkeyBtn || !statusBox || !window.isSecureContext || !window.PublicKeyCredential || !navigator.credentials) return;

  if (divider) divider.hidden = false;
  if (panel) panel.hidden = false;

  const defaultLabel = passkeyBtn.dataset.label || "الدخول بالبصمة";
  let autofillController = null;
  let busy = false;

  function showStatus(message, isError) {
    statusBox.textContent = message;
    statusBox.classList.toggle("is-error", Boolean(isError));
    statusBox.hidden = !message;
  }

  function setBusy(isBusy, label) {
    busy = isBusy;
    passkeyBtn.disabled = isBusy;
    passkeyBtn.setAttribute("aria-busy", isBusy ? "true" : "false");
    if (labelSpan) labelSpan.textContent = isBusy ? (label || "جارٍ التحقق...") : defaultLabel;
  }

  function passkeyErrorMessage(error, scoped) {
    const site = window.location.origin;
    if (error && (error.name === "NotSupportedError" || error.name === "ConstraintError")) {
      return "هذا الجهاز أو المتصفح لا يدعم الدخول بمفتاح المرور. سجّل الدخول بكلمة المرور، أو حدّث Chrome أو Safari وفعّل قفل الشاشة.";
    }
    if (error && error.name === "SecurityError") {
      return "تعذر التحقق من نطاق المنصة. افتح " + site + " مباشرة في Chrome أو Safari ثم أعد المحاولة.";
    }
    if (error && (error.name === "NotAllowedError" || error.name === "AbortError")) {
      return scoped
        ? "لم يكتمل الدخول. تأكد من أن هذا الجهاز هو الذي فعّلت عليه البصمة، ثم أعد المحاولة."
        : "لم نجد مفتاح مرور محفوظًا على هذا الجهاز. اكتب رقم الجوال ثم أعد المحاولة، أو استخدم كلمة المرور.";
    }
    return "تعذر تسجيل الدخول بمفتاح المرور. أعد المحاولة أو استخدم كلمة المرور.";
  }

  function b64ToBuffer(value) {
    const base64 = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - base64.length % 4) % 4);
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes.buffer;
  }

  function bufferToB64(buffer) {
    const bytes = new Uint8Array(buffer || new ArrayBuffer(0));
    let binary = "";
    bytes.forEach(function (byte) {
      binary += String.fromCharCode(byte);
    });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function prepareRequestOptions(publicKey) {
    publicKey.challenge = b64ToBuffer(publicKey.challenge);
    if (publicKey.allowCredentials) {
      publicKey.allowCredentials = publicKey.allowCredentials.map(function (item) {
        return Object.assign({}, item, { id: b64ToBuffer(item.id) });
      });
    }
    return publicKey;
  }

  function credentialToJSON(credential) {
    return {
      id: credential.id,
      rawId: bufferToB64(credential.rawId),
      type: credential.type,
      response: {
        authenticatorData: bufferToB64(credential.response.authenticatorData),
        clientDataJSON: bufferToB64(credential.response.clientDataJSON),
        signature: bufferToB64(credential.response.signature),
        userHandle: credential.response.userHandle ? bufferToB64(credential.response.userHandle) : null,
      },
      next: nextInput ? nextInput.value : "",
    };
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfInput ? csrfInput.value : "",
      },
      body: JSON.stringify(payload || {}),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "تعذر تنفيذ العملية.");
    return data;
  }

  async function completeLogin(credential) {
    const verified = await postJSON(passkeyBtn.dataset.verifyUrl, credentialToJSON(credential));
    showStatus("تم التحقق، جارٍ فتح حسابك...", false);
    window.location.href = verified.redirect || "/";
  }

  async function startAutofill() {
    if (typeof PublicKeyCredential.isConditionalMediationAvailable !== "function") return;
    try {
      if (!(await PublicKeyCredential.isConditionalMediationAvailable())) return;
    } catch (error) {
      return;
    }

    const controller = new AbortController();
    autofillController = controller;
    try {
      const options = await postJSON(passkeyBtn.dataset.optionsUrl, {});
      const credential = await navigator.credentials.get({
        publicKey: prepareRequestOptions(options.publicKey),
        mediation: "conditional",
        signal: controller.signal,
      });
      if (!credential) return;
      setBusy(true, "جارٍ التحقق...");
      await completeLogin(credential);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error && (error.name === "NotAllowedError" || error.name === "AbortError")) return;
    } finally {
      if (autofillController === controller) autofillController = null;
    }
  }

  passkeyBtn.addEventListener("click", async function () {
    if (busy) return;
    const identifierValue = identifier ? identifier.value.trim() : "";
    const scoped = Boolean(identifierValue);

    if (autofillController) {
      autofillController.abort();
      autofillController = null;
    }

    try {
      setBusy(true, "في انتظار تأكيد الجهاز...");
      showStatus("أكمل التحقق ببصمتك أو Face ID أو قفل الشاشة...", false);
      const options = await postJSON(passkeyBtn.dataset.optionsUrl, { identifier: identifierValue });
      const credential = await navigator.credentials.get({ publicKey: prepareRequestOptions(options.publicKey) });
      if (!credential) throw new DOMException("Credential request returned no result", "NotAllowedError");
      await completeLogin(credential);
    } catch (error) {
      showStatus(passkeyErrorMessage(error, scoped), true);
      setBusy(false);
      startAutofill();
    }
  });

  startAutofill();
})();
