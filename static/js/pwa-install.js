(function () {
  "use strict";

  if (window.__tawtheeqPwaInstallerLoaded) return;
  window.__tawtheeqPwaInstallerLoaded = true;

  var SW_URL = "/sw.js?v=12";
  var INSTALLED_KEY = "tawtheeq_pwa_installed_v1";
  var SESSION_DISMISSED_KEY = "tawtheeq_pwa_install_dismissed_session_v1";
  var TASK_COMPLETE_KEY = "tawtheeq_pwa_task_completed_v1";
  var AUTO_NATIVE_DELAY_MS = 1200;
  var AUTO_IOS_DELAY_MS = 1800;
  var AUTO_FALLBACK_DELAY_MS = 2400;

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register(SW_URL, {
        scope: "/",
        updateViaCache: "none"
      }).then(function (registration) {
        function announceUpdate(worker) {
          window.dispatchEvent(new CustomEvent("tawtheeq:pwa-update", { detail: { worker: worker } }));
        }
        if (registration.waiting && navigator.serviceWorker.controller) announceUpdate(registration.waiting);
        registration.addEventListener("updatefound", function () {
          var worker = registration.installing;
          if (!worker) return;
          worker.addEventListener("statechange", function () {
            if (worker.state === "installed" && navigator.serviceWorker.controller) announceUpdate(worker);
          });
        });
        registration.update().catch(function () {});
      }).catch(function () {});
    });
  }

  var promptRoot = document.getElementById("pwaInstallPrompt");
  var installAction = document.getElementById("pwaInstallAction");
  var closeButton = document.getElementById("pwaInstallClose");
  var laterButton = document.getElementById("pwaInstallLater");
  var description = document.getElementById("pwaInstallDescription");
  var announcement = document.getElementById("pwaInstallAnnouncement");
  var steps = document.getElementById("pwaInstallSteps");
  var installTriggers = Array.prototype.slice.call(
    document.querySelectorAll("[data-pwa-install-trigger]")
  );
  var autoPromptAllowed = Boolean(
    promptRoot && promptRoot.getAttribute("data-auto-prompt") === "true"
  );

  var userAgent = navigator.userAgent || "";
  var isIPadOS = /macintosh/i.test(userAgent) && navigator.maxTouchPoints > 1;
  var isIOS = /iphone|ipad|ipod/i.test(userAgent) || isIPadOS;
  var isAndroid = /android/i.test(userAgent);
  var isSafari = isIOS && /safari/i.test(userAgent) && !/crios|fxios|edgios|opios/i.test(userAgent);
  var userAgentReportsMobile = Boolean(
    navigator.userAgentData && navigator.userAgentData.mobile
  );
  var hasCoarsePointer = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
  var hasMobileWidth = window.matchMedia && window.matchMedia("(max-width: 1024px)").matches;
  var isMobile =
    isIOS ||
    isAndroid ||
    userAgentReportsMobile ||
    /mobile|phone|opera mini|iemobile/i.test(userAgent) ||
    (hasCoarsePointer && hasMobileWidth);
  var isStandalone =
    (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
    (window.matchMedia && window.matchMedia("(display-mode: fullscreen)").matches) ||
    Boolean(window.navigator.standalone);
  var deferredPrompt = null;
  var knownInstalled = isStandalone;

  function getLocalFlag(key) {
    try {
      return window.localStorage.getItem(key) === "true";
    } catch (error) {
      return false;
    }
  }

  function setLocalFlag(key, value) {
    try {
      if (value) window.localStorage.setItem(key, "true");
      else window.localStorage.removeItem(key);
    } catch (error) {}
  }

  function wasDismissedThisSession() {
    try {
      return window.sessionStorage.getItem(SESSION_DISMISSED_KEY) === "true";
    } catch (error) {
      return false;
    }
  }

  function rememberSessionDismissal() {
    try {
      window.sessionStorage.setItem(SESSION_DISMISSED_KEY, "true");
    } catch (error) {}
  }

  function clearSessionDismissal() {
    try {
      window.sessionStorage.removeItem(SESSION_DISMISSED_KEY);
    } catch (error) {}
  }

  function markInstalled() {
    knownInstalled = true;
    setLocalFlag(INSTALLED_KEY, true);
  }

  function markNotInstalled() {
    knownInstalled = false;
    setLocalFlag(INSTALLED_KEY, false);
  }

  function isKnownInstalled() {
    return isStandalone || knownInstalled;
  }

  function hasCompletedTask() {
    try { return window.localStorage.getItem(TASK_COMPLETE_KEY) === "true"; }
    catch (error) { return false; }
  }

  knownInstalled = isStandalone || getLocalFlag(INSTALLED_KEY);
  if (isStandalone) markInstalled();

  // Visiting the login/public page starts a fresh login journey. If the user
  // previously chose "Later", the reminder may appear again after this login.
  if (!autoPromptAllowed) clearSessionDismissal();

  if (!promptRoot || !installAction || !closeButton || !laterButton || !description || !announcement || !steps) {
    installTriggers.forEach(function (trigger) { trigger.hidden = true; });
    window.TawtheeqPWA = { isStandalone: isStandalone, isInstalled: isKnownInstalled(), canInstall: false };
    return;
  }

  function hideInstalledExperience() {
    promptRoot.hidden = true;
    promptRoot.setAttribute("aria-hidden", "true");
    installTriggers.forEach(function (trigger) { trigger.hidden = true; });
  }

  function checkInstalledRelatedApps() {
    if (isStandalone) return Promise.resolve(true);
    if (typeof navigator.getInstalledRelatedApps !== "function") {
      return Promise.resolve(knownInstalled);
    }

    return navigator.getInstalledRelatedApps().then(function (apps) {
      if (apps && apps.length > 0) {
        markInstalled();
        hideInstalledExperience();
        return true;
      }

      // On supported browsers an empty result is stronger evidence than an
      // old local marker, and also recovers correctly after uninstalling.
      markNotInstalled();
      return false;
    }).catch(function () {
      return knownInstalled;
    });
  }

  function closeMobileDrawer() {
    var drawerClose = document.getElementById("drawerClose");
    if (drawerClose && document.getElementById("mobileDrawer") &&
        document.getElementById("mobileDrawer").classList.contains("open")) {
      drawerClose.click();
    }
  }

  function setSteps(items) {
    steps.innerHTML = "";
    items.forEach(function (item) {
      var listItem = document.createElement("li");
      listItem.textContent = item;
      steps.appendChild(listItem);
    });
  }

  function configureNativePrompt() {
    steps.hidden = true;
    description.textContent = "ثبّت منصة توثيق للوصول السريع وفتحها بواجهة مستقلة من شاشتك الرئيسية.";
    installAction.textContent = "تثبيت الآن";
  }

  function configureFallback() {
    deferredPrompt = null;
    steps.hidden = false;

    if (isIOS && isSafari) {
      description.textContent = "أضف منصة توثيق إلى شاشة iPhone أو iPad الرئيسية وافتحها كتطبيق مستقل.";
      setSteps([
        "اضغط زر المشاركة في Safari.",
        "اختر «إضافة إلى الشاشة الرئيسية».",
        "فعّل «فتح كتطبيق ويب» ثم اضغط «إضافة»."
      ]);
      installAction.textContent = "حسنًا";
      return;
    }

    if (isIOS) {
      description.textContent = "للتثبيت على iPhone أو iPad افتح هذه الصفحة في Safari أولًا.";
      setSteps([
        "انسخ رابط الصفحة وافتحه في Safari.",
        "اضغط مشاركة ثم «إضافة إلى الشاشة الرئيسية».",
        "فعّل «فتح كتطبيق ويب» ثم اضغط «إضافة»."
      ]);
      installAction.textContent = "حسنًا";
      return;
    }

    description.textContent = "يمكنك إضافة منصة توثيق من قائمة المتصفح إلى الشاشة الرئيسية.";
    setSteps([
      "افتح قائمة المتصفح ⋮.",
      "اختر «تثبيت التطبيق» أو «إضافة إلى الشاشة الرئيسية».",
      "وافق على الإضافة."
    ]);
    installAction.textContent = "حسنًا";
  }

  function showPrompt(options) {
    options = options || {};
    if (
      isKnownInstalled() ||
      (!options.explicit && (
        !autoPromptAllowed || !isMobile || !hasCompletedTask() || wasDismissedThisSession()
      ))
    ) return false;
    promptRoot.hidden = false;
    promptRoot.setAttribute("aria-hidden", "false");
    announcement.textContent = "";
    window.setTimeout(function () {
      if (!promptRoot.hidden) {
        announcement.textContent = "يتوفر تثبيت منصة توثيق على هذا الجوال. " + description.textContent;
      }
    }, 80);
    return true;
  }

  function hidePrompt(dismissForSession) {
    promptRoot.hidden = true;
    promptRoot.setAttribute("aria-hidden", "true");
    announcement.textContent = "";
    if (dismissForSession) rememberSessionDismissal();
  }

  function showInstallPrompt(explicit) {
    if (isKnownInstalled()) return false;
    if (deferredPrompt) configureNativePrompt();
    else configureFallback();
    return showPrompt({ explicit: Boolean(explicit) });
  }

  window.TawtheeqPWA = {
    isStandalone: isStandalone,
    isInstalled: isKnownInstalled(),
    canInstall: !isKnownInstalled() && isMobile,
    showInstallPrompt: function () { return showInstallPrompt(true); }
  };

  installTriggers.forEach(function (trigger) {
    if (isKnownInstalled()) {
      trigger.hidden = true;
      return;
    }
    // البطاقة ونصّها مكتوبان للجوال («ثبّت على الجوال»، خطوات سفاري). الزرّ
    // المعروض للزائر يحمل هذه السمة فيختفي على سطح المكتب، بدل تكرار حساب
    // «هل هذا جوال؟» في CSS بمعيارٍ ثانٍ قد يخالف ما يراه الجافاسكربت.
    if (!isMobile && trigger.hasAttribute("data-pwa-install-mobile-only")) {
      trigger.hidden = true;
      return;
    }
    trigger.addEventListener("click", function () {
      closeMobileDrawer();
      window.setTimeout(function () { showInstallPrompt(true); }, 120);
    });
  });

  if (!isStandalone) {
    window.addEventListener("beforeinstallprompt", function (event) {
      event.preventDefault();
      // A fresh install prompt means the browser currently considers this PWA
      // installable, so discard any stale marker left by an earlier install.
      markNotInstalled();
      deferredPrompt = event;
      configureNativePrompt();
      if (autoPromptAllowed && hasCompletedTask()) {
        window.setTimeout(function () {
          if (deferredPrompt && document.visibilityState === "visible") showPrompt();
        }, AUTO_NATIVE_DELAY_MS);
      }
    });
  }

  installAction.addEventListener("click", function () {
    if (!deferredPrompt) {
      hidePrompt(true);
      return;
    }

    var installEvent = deferredPrompt;
    deferredPrompt = null;
    installEvent.prompt();
    installEvent.userChoice.then(function (choice) {
      if (choice && choice.outcome === "accepted") {
        markInstalled();
        hideInstalledExperience();
      } else {
        hidePrompt(true);
      }
    }).catch(function () {
      configureFallback();
      showPrompt({ explicit: true });
    });
  });

  closeButton.addEventListener("click", function () { hidePrompt(true); });
  laterButton.addEventListener("click", function () { hidePrompt(true); });

  document.addEventListener("keydown", function (event) {
    if (promptRoot.hidden) return;
    if (event.key === "Escape") {
      hidePrompt(true);
    }
  });

  window.addEventListener("appinstalled", function () {
    deferredPrompt = null;
    markInstalled();
    hideInstalledExperience();
  });

  window.addEventListener("tawtheeq:task-complete", function () {
    try { window.localStorage.setItem(TASK_COMPLETE_KEY, "true"); } catch (error) {}
    if (!isKnownInstalled() && autoPromptAllowed && isMobile && !wasDismissedThisSession()) {
      window.setTimeout(function () { showInstallPrompt(false); }, AUTO_NATIVE_DELAY_MS);
    }
  });

  checkInstalledRelatedApps().then(function (installed) {
    window.TawtheeqPWA.isInstalled = installed;
    window.TawtheeqPWA.canInstall = !installed && isMobile;
    if (installed || !autoPromptAllowed || !isMobile || !hasCompletedTask() || wasDismissedThisSession()) return;

    // iOS does not emit beforeinstallprompt, while some Android and alternative
    // mobile browsers may not emit it either. Always retain a manual path.
    window.setTimeout(function () {
      if (promptRoot.hidden && document.visibilityState === "visible") {
        if (deferredPrompt) configureNativePrompt();
        else configureFallback();
        showPrompt();
      }
    }, isIOS ? AUTO_IOS_DELAY_MS : AUTO_FALLBACK_DELAY_MS);
  });
}());
