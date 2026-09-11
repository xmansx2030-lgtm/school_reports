(function () {
  "use strict";

  if (window.__tawtheeqWebPushLoaded) return;
  window.__tawtheeqWebPushLoaded = true;

  var root = document.getElementById("webPushPrompt");
  var promptTriggers = Array.prototype.slice.call(
    document.querySelectorAll("[data-web-push-trigger]")
  );
  if (!root) return;

  var enableButton = document.getElementById("webPushEnable");
  var laterButton = document.getElementById("webPushLater");
  var closeButton = document.getElementById("webPushPromptClose");
  var titleNode = document.getElementById("webPushPromptTitle");
  var descriptionNode = document.getElementById("webPushPromptDescription");
  var benefitsNode = root.querySelector(".web-push-prompt__benefits");
  var statusNode = document.getElementById("webPushPromptStatus");
  var configUrl = root.getAttribute("data-config-url");
  var subscribeUrl = root.getAttribute("data-subscribe-url");
  var DISMISSED_UNTIL_KEY = "tawtheeq_web_push_dismissed_until_v1";
  var DISMISS_DAYS = 30;
  var SHOW_DELAY_MS = 25000;
  var supported = Boolean(
    "serviceWorker" in navigator && "PushManager" in window && "Notification" in window
  );
  var userAgent = navigator.userAgent || "";
  var isIPadOS = /macintosh/i.test(userAgent) && navigator.maxTouchPoints > 1;
  var isIOS = /iphone|ipad|ipod/i.test(userAgent) || isIPadOS;
  var config = null;
  var configRequest = null;

  function storedNumber(key) {
    try { return Number(window.localStorage.getItem(key) || 0); } catch (error) { return 0; }
  }

  function rememberDismissal(days) {
    try {
      window.localStorage.setItem(DISMISSED_UNTIL_KEY, String(Date.now() + days * 86400000));
    } catch (error) {}
  }

  function isDismissed() { return storedNumber(DISMISSED_UNTIL_KEY) > Date.now(); }

  function isStandalone() {
    return Boolean(
      (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
      (window.matchMedia && window.matchMedia("(display-mode: fullscreen)").matches) ||
      window.navigator.standalone
    );
  }

  function iosNeedsInstallation() { return isIOS && !isStandalone(); }

  function currentPermission() {
    return supported ? Notification.permission : "unsupported";
  }

  function csrfToken() {
    var field = document.querySelector("#__csrf input[name='csrfmiddlewaretoken']");
    return field ? field.value : "";
  }

  function closeMobileDrawer() {
    var drawer = document.getElementById("mobileDrawer");
    var drawerClose = document.getElementById("drawerClose");
    if (drawer && drawerClose && drawer.classList.contains("open")) drawerClose.click();
  }

  function setTriggerState(state, label) {
    promptTriggers.forEach(function (trigger) {
      trigger.setAttribute("data-push-state", state);
      trigger.setAttribute("aria-label", label);
      var labelNode = trigger.querySelector("[data-web-push-trigger-label]");
      if (labelNode) labelNode.textContent = label;
    });
  }

  function updateTriggerState() {
    var permission = currentPermission();
    if (!supported) setTriggerState("unsupported", "الإشعارات غير مدعومة");
    else if (iosNeedsInstallation()) setTriggerState("install", "ثبّت التطبيق للإشعارات");
    else if (permission === "granted") setTriggerState("enabled", "الإشعارات مفعّلة");
    else if (permission === "denied") setTriggerState("blocked", "الإشعارات محظورة");
    else setTriggerState("available", "إشعارات الجهاز");
  }

  function configurePanel() {
    var permission = currentPermission();
    enableButton.disabled = false;
    benefitsNode.hidden = false;
    statusNode.textContent = "";

    if (!supported) {
      titleNode.textContent = "الإشعارات غير مدعومة";
      descriptionNode.textContent = "حدّث نظام الجهاز والمتصفح، ثم افتح المنصة مرة أخرى.";
      enableButton.hidden = true;
      benefitsNode.hidden = true;
      return;
    }

    enableButton.hidden = false;
    if (iosNeedsInstallation()) {
      titleNode.textContent = "ثبّت التطبيق لتفعيل الإشعارات";
      descriptionNode.textContent = "في iPhone وiPad تعمل الإشعارات بعد إضافة المنصة إلى الشاشة الرئيسية وفتحها كتطبيق.";
      enableButton.textContent = "تثبيت التطبيق أولًا";
      return;
    }

    if (permission === "denied") {
      titleNode.textContent = "الإشعارات محظورة من الجهاز";
      descriptionNode.textContent = "اسمح بالإشعارات لمنصة توثيق من إعدادات المتصفح أو إعدادات التطبيق، ثم عد إلى هذه الشاشة.";
      enableButton.textContent = "تحقق مرة أخرى";
      benefitsNode.hidden = true;
      return;
    }

    if (permission === "granted") {
      titleNode.textContent = "إشعارات الجهاز مفعّلة";
      descriptionNode.textContent = "سنتحقق من ربط هذا الجهاز بحسابك حتى تستمر التنبيهات عند إغلاق التطبيق.";
      enableButton.textContent = "تحقق من الربط";
      return;
    }

    titleNode.textContent = "فعّل إشعارات الجوال";
    descriptionNode.textContent = "ستصل التنبيهات المهمة إلى شاشة القفل حتى عندما تكون منصة توثيق مغلقة.";
    enableButton.textContent = "تفعيل الإشعارات";
  }

  function hide(days) {
    root.hidden = true;
    root.setAttribute("aria-hidden", "true");
    if (days) rememberDismissal(days);
  }

  function show(options) {
    options = options || {};
    if (!options.explicit && (
      currentPermission() !== "default" || isDismissed() || iosNeedsInstallation()
    )) return false;
    if (!options.explicit) {
      var installPrompt = document.getElementById("pwaInstallPrompt");
      if (installPrompt && !installPrompt.hidden) {
        window.setTimeout(function () { show(); }, 15000);
        return false;
      }
    }
    configurePanel();
    root.hidden = false;
    root.setAttribute("aria-hidden", "false");
    return true;
  }

  function base64UrlToUint8Array(value) {
    var padding = "=".repeat((4 - value.length % 4) % 4);
    var raw = window.atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, function (character) { return character.charCodeAt(0); });
  }

  function loadConfig() {
    if (config) return Promise.resolve(config);
    if (configRequest) return configRequest;
    configRequest = window.fetch(configUrl, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (!response.ok) {
          var error = new Error("config_failed");
          error.status = response.status;
          throw error;
        }
        return response.json();
      }).then(function (value) {
        config = value;
        return value;
      }).catch(function (error) {
        configRequest = null;
        throw error;
      });
    return configRequest;
  }

  function postSubscription(subscription) {
    return window.fetch(subscribeUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify({ subscription: subscription.toJSON() })
    }).then(function (response) {
      if (response.ok) return response.json();
      return response.json().catch(function () { return {}; }).then(function (payload) {
        var error = new Error(payload.error || "subscription_sync_failed");
        error.status = response.status;
        throw error;
      });
    });
  }

  function registrationReady() {
    return navigator.serviceWorker.getRegistration("/").then(function (registration) {
      if (registration) return registration;
      return navigator.serviceWorker.register("/sw.js?v=12", { scope: "/", updateViaCache: "none" });
    }).then(function () { return navigator.serviceWorker.ready; });
  }

  function subscribeCurrentDevice() {
    return loadConfig().then(function (value) {
      if (!value.enabled || !value.publicKey) throw new Error("push_not_configured");
      return registrationReady().then(function (registration) {
        return registration.pushManager.getSubscription().then(function (subscription) {
          if (subscription) return subscription;
          return registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: base64UrlToUint8Array(value.publicKey)
          });
        });
      });
    }).then(function (subscription) {
      return postSubscription(subscription).then(function () { return subscription; });
    });
  }

  function finishEnable() {
    statusNode.textContent = "تم التفعيل وربط هذا الجهاز بنجاح.";
    try { window.localStorage.removeItem(DISMISSED_UNTIL_KEY); } catch (error) {}
    updateTriggerState();
    window.setTimeout(function () { hide(); }, 1800);
  }

  function reportEnableError(error) {
    if (currentPermission() === "denied" || error.message === "permission_denied") {
      statusNode.textContent = "الإذن محظور. اسمح به من إعدادات المتصفح أو التطبيق ثم اضغط «تحقق مرة أخرى».";
      rememberDismissal(180);
    } else if (error.message === "push_not_configured") {
      statusNode.textContent = "خدمة الإشعارات غير مهيأة حاليًا. تواصل مع إدارة المنصة.";
    } else if (error.message === "config_failed") {
      statusNode.textContent = "تعذر الوصول إلى خدمة ربط الإشعارات. أعد تحميل التطبيق ثم حاول مرة أخرى.";
    } else if (error.message === "push_endpoint_not_allowed") {
      statusNode.textContent = "مزود الإشعارات في هذا الجهاز غير مدعوم حاليًا. حدّث التطبيق ثم حاول مرة أخرى.";
    } else {
      statusNode.textContent = "تعذر ربط الجهاز الآن. تحقق من الاتصال ثم حاول مرة أخرى.";
    }
    updateTriggerState();
  }

  function enable() {
    if (!supported) return;
    if (iosNeedsInstallation()) {
      statusNode.textContent = "بعد التثبيت افتح منصة توثيق من أيقونتها ثم فعّل الإشعارات.";
      if (window.TawtheeqPWA && window.TawtheeqPWA.showInstallPrompt) {
        hide();
        window.TawtheeqPWA.showInstallPrompt();
      }
      return;
    }

    enableButton.disabled = true;
    statusNode.textContent = currentPermission() === "granted"
      ? "جارٍ التحقق من ربط هذا الجهاز…"
      : "جارٍ طلب إذن الإشعارات…";

    var permissionRequest = currentPermission() === "granted"
      ? Promise.resolve("granted")
      : Notification.requestPermission();

    Promise.resolve(permissionRequest).then(function (permission) {
      if (permission !== "granted") throw new Error("permission_denied");
      return subscribeCurrentDevice();
    }).then(finishEnable).catch(reportEnableError).then(function () {
      enableButton.disabled = false;
    });
  }

  window.TawtheeqPush = {
    enable: enable,
    sync: subscribeCurrentDevice,
    showPrompt: function () { return show({ explicit: true }); },
    isSupported: supported
  };

  enableButton.addEventListener("click", enable);
  laterButton.addEventListener("click", function () { hide(DISMISS_DAYS); });
  closeButton.addEventListener("click", function () { hide(DISMISS_DAYS); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !root.hidden) hide(DISMISS_DAYS);
  });

  promptTriggers.forEach(function (trigger) {
    trigger.addEventListener("click", function () {
      closeMobileDrawer();
      window.setTimeout(function () { show({ explicit: true }); }, 120);
    });
  });
  updateTriggerState();

  loadConfig().then(function (value) {
    if (!value.enabled) {
      promptTriggers.forEach(function (trigger) { trigger.hidden = true; });
      return;
    }
    if (supported && Notification.permission === "granted") {
      subscribeCurrentDevice().then(updateTriggerState).catch(function () {});
    } else if (currentPermission() === "default" && !iosNeedsInstallation()) {
      window.setTimeout(function () { show(); }, SHOW_DELAY_MS);
    }
  }).catch(function () {});

  window.addEventListener("appinstalled", function () {
    updateTriggerState();
    if (currentPermission() === "default") window.setTimeout(function () { show(); }, 4000);
  });
}());
