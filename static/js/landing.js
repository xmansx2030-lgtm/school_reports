(function () {
  "use strict";

  var menu = document.getElementById("mobileMenu");
  var menuToggle = document.getElementById("menuToggle");
  var menuClose = document.getElementById("menuClose");
  var menuPreviousFocus = null;
  var menuInertState = [];

  function visibleFocusable(element) {
    return element.getClientRects().length > 0
      && window.getComputedStyle(element).visibility !== "hidden"
      && !element.closest("[inert]");
  }

  function menuFocusables() {
    if (!menu) return [];
    return Array.prototype.filter.call(
      menu.querySelectorAll(
        'a[href], button:not(:disabled), input:not(:disabled), '
        + 'select:not(:disabled), textarea:not(:disabled), [tabindex]'
      ),
      function (element) {
        return element.tabIndex >= 0 && visibleFocusable(element);
      }
    );
  }

  function focusMenu() {
    var first = menuFocusables()[0];
    if (first) first.focus();
    else if (menu) {
      if (!menu.hasAttribute("tabindex")) menu.setAttribute("tabindex", "-1");
      menu.focus();
    }
  }

  function setBackgroundInert() {
    // Walk ancestors as well, so the drawer may live inside a shared shell.
    var branch = menu;
    while (branch && branch !== document.body) {
      var parent = branch.parentElement;
      if (!parent) break;
      Array.prototype.forEach.call(parent.children, function (sibling) {
        if (sibling === branch) return;
        menuInertState.push({
          element: sibling,
          value: sibling.getAttribute("inert")
        });
        sibling.setAttribute("inert", "");
      });
      branch = parent;
    }
  }

  function restoreBackground() {
    menuInertState.forEach(function (state) {
      if (state.value === null) state.element.removeAttribute("inert");
      else state.element.setAttribute("inert", state.value);
    });
    menuInertState = [];
  }

  function setMenu(open, restoreFocus) {
    if (!menu || !menuToggle) return;
    if (open === !menu.hidden) return;
    if (open) menuPreviousFocus = document.activeElement;
    menu.hidden = !open;
    menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.classList.toggle("menu-open", open);
    if (open) {
      setBackgroundInert();
      focusMenu();
    } else {
      restoreBackground();
      if (restoreFocus !== false) {
        var returnTarget = menuPreviousFocus;
        if (!returnTarget || !returnTarget.isConnected || !visibleFocusable(returnTarget)) {
          returnTarget = menuToggle;
        }
        if (visibleFocusable(returnTarget)) returnTarget.focus({ preventScroll: true });
      }
      menuPreviousFocus = null;
    }
  }

  function hashTarget(hash) {
    if (!hash || hash === "#") return null;
    try {
      return document.getElementById(decodeURIComponent(hash.slice(1)));
    } catch (error) {
      return null;
    }
  }

  function sectionLinkTarget(link) {
    if (!link || !link.hasAttribute("href")) return null;
    var url = new URL(link.href, window.location.href);
    if (
      url.origin !== window.location.origin
      || url.pathname !== window.location.pathname
      || url.search !== window.location.search
    ) return null;
    return hashTarget(url.hash);
  }

  function focusSection(target) {
    if (!target) return;
    var needsTabindex = !target.hasAttribute("tabindex") && target.tabIndex < 0;
    if (needsTabindex) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
    if (needsTabindex) {
      target.addEventListener("blur", function cleanup() {
        target.removeAttribute("tabindex");
        target.removeEventListener("blur", cleanup);
      });
    }
  }

  if (menuToggle) {
    menuToggle.addEventListener("click", function () {
      setMenu(true);
    });
  }
  if (menuClose) {
    menuClose.addEventListener("click", function () {
      setMenu(false);
    });
  }
  if (menu) {
    menu.addEventListener("click", function (event) {
      var link = event.target.closest && event.target.closest("a[href]");
      if (link && menu.contains(link)) {
        var destination = sectionLinkTarget(link);
        setMenu(false, !destination);
        if (destination) {
          window.requestAnimationFrame(function () { focusSection(destination); });
        }
      } else if (event.target === menu) {
        setMenu(false);
      }
    });
  }
  document.addEventListener("keydown", function (event) {
    if (!menu || menu.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setMenu(false);
    } else if (event.key === "Tab") {
      var focusables = menuFocusables();
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      var active = document.activeElement;
      if (!first) {
        event.preventDefault();
        focusMenu();
      } else if (event.shiftKey && (active === first || !menu.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !menu.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  document.addEventListener("focusin", function (event) {
    if (menu && !menu.hidden && !menu.contains(event.target)) focusMenu();
  });
  window.addEventListener("resize", function () {
    if (menu && !menu.hidden && menuToggle && !menuToggle.getClientRects().length) {
      setMenu(false, false);
      var brand = document.querySelector(".site-header a[href], header a[href]");
      if (brand && visibleFocusable(brand)) brand.focus({ preventScroll: true });
    }
  });

  var periodButtons = document.querySelectorAll(".period-switch [data-period]");
  Array.prototype.forEach.call(periodButtons, function (button) {
    button.addEventListener("click", function () {
      var period = button.getAttribute("data-period");

      Array.prototype.forEach.call(periodButtons, function (item) {
        var active = item.getAttribute("data-period") === period;
        item.classList.toggle("active", active);
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", active ? "true" : "false");
      });

      var flexiblePricing = document.querySelector("[data-flex-pricing]");
      if (flexiblePricing && flexiblePricing.__flexiblePricing) {
        flexiblePricing.__flexiblePricing.setPeriod(period);
      }

      Array.prototype.forEach.call(
        document.querySelectorAll("[data-price-card]"),
        function (card) {
          var requested = card.querySelector(
            '[data-period-content="' + period + '"]'
          );
          var fallback = card.querySelector("[data-period-content]");
          Array.prototype.forEach.call(
            card.querySelectorAll("[data-period-content]"),
            function (panel) {
              panel.classList.toggle(
                "active",
                panel === (requested || fallback)
              );
            }
          );
        }
      );
    });
  });

  Array.prototype.forEach.call(
    document.querySelectorAll("details"),
    function (detail) {
      detail.addEventListener("toggle", function () {
        var icon = detail.querySelector("summary i");
        if (icon) {
          icon.classList.toggle("fa-plus", !detail.open);
          icon.classList.toggle("fa-minus", detail.open);
        }
      });
    }
  );

  var switches = [];

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-switch-group]"),
    function (group) {
      function ownElements(selector) {
        return Array.prototype.filter.call(group.querySelectorAll(selector), function (element) {
          return element.closest("[data-switch-group]") === group;
        });
      }

      var panels = ownElements("[data-switch-panel]");
      var tabs = ownElements("[data-switch-tab]").filter(function (tab) {
        return panels.some(function (panel) {
          return panel.id === tab.getAttribute("aria-controls");
        });
      });
      if (!tabs.length) return;

      function activate(tab, moveFocus) {
        var targetId = tab.getAttribute("aria-controls");
        tabs.forEach(function (item) {
          var active = item === tab;
          item.setAttribute("role", "tab");
          item.classList.toggle("is-active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
          item.setAttribute("tabindex", active ? "0" : "-1");
        });
        panels.forEach(function (panel) {
          var active = panel.id === targetId;
          panel.hidden = !active;
          panel.classList.toggle("is-active", active);
          panel.setAttribute("role", "tabpanel");
        });
        if (moveFocus) tab.focus();
      }

      function tabForTarget(target) {
        if (!target) return null;
        return tabs.filter(function (tab) {
          var panel = document.getElementById(tab.getAttribute("aria-controls"));
          return panel === target || panel.contains(target);
        })[0] || null;
      }

      tabs.forEach(function (tab, index) {
        var panel = document.getElementById(tab.getAttribute("aria-controls"));
        if (tab.id) panel.setAttribute("aria-labelledby", tab.id);
        tab.addEventListener("click", function () { activate(tab, false); });
        tab.addEventListener("keydown", function (event) {
          var nextIndex = index;
          var rtl = window.getComputedStyle(group).direction === "rtl";
          if (event.key === "ArrowLeft") nextIndex += rtl ? 1 : -1;
          else if (event.key === "ArrowRight") nextIndex += rtl ? -1 : 1;
          else if (event.key === "Home") nextIndex = 0;
          else if (event.key === "End") nextIndex = tabs.length - 1;
          else return;
          event.preventDefault();
          activate(tabs[(nextIndex + tabs.length) % tabs.length], true);
        });
      });

      var initialTab = tabForTarget(hashTarget(window.location.hash))
        || tabs.filter(function (tab) { return tab.getAttribute("aria-selected") === "true"; })[0]
        || tabs[0];
      activate(initialTab, false);
      ownElements("[data-switch-tabs]").forEach(function (tablist) {
        tablist.setAttribute("role", "tablist");
        tablist.hidden = false;
      });
      switches.push({ activate: activate, tabForTarget: tabForTarget });
    }
  );

  function activateTarget(target) {
    switches.forEach(function (group) {
      var tab = group.tabForTarget(target);
      if (tab) group.activate(tab, false);
    });
  }

  // Reveal a deep-linked panel before the browser performs its anchor scroll.
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
    var target = sectionLinkTarget(link);
    if (target) activateTarget(target);
  });

  function syncHashTarget() {
    var target = hashTarget(window.location.hash);
    if (!target) return;
    activateTarget(target);
    window.requestAnimationFrame(function () {
      target.scrollIntoView({ block: "start", behavior: "instant" });
    });
  }
  window.addEventListener("hashchange", syncHashTarget);
  if (window.location.hash) syncHashTarget();

  if (document.body.classList.contains("landing-page")) {
    document.body.classList.add("landing-assistant-delayed");
    var syncLandingScrollState = function () {
      document.body.classList.toggle(
        "landing-page-has-scrolled",
        window.scrollY > 320
      );
      // حالةٌ ثانية بعتبةٍ مختلفة عن قصد: الشريط العلوي يكتسب حدَّه وظلَّه بمجرد
      // أن يبدأ المحتوى بالمرور تحته، بينما عتبة 320 أعلاه تخصّ ظهور مُشغّل
      // المساعد — ربطُهما بعتبةٍ واحدة يجعل أحدهما يتأخّر أو الآخر يتعجّل.
      document.body.classList.toggle("landing-page-is-lifted", window.scrollY > 8);
    };
    syncLandingScrollState();
    window.addEventListener("scroll", syncLandingScrollState, { passive: true });
  }

  var lightbox = document.getElementById("productLightbox");
  var lightboxImage = document.getElementById("productLightboxImage");
  var lightboxCaption = document.getElementById("productLightboxCaption");
  var lightboxClose = document.getElementById("productLightboxClose");

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-product-image]"),
    function (trigger) {
      trigger.addEventListener("click", function () {
        var source = trigger.getAttribute("data-product-image");
        var image = trigger.querySelector("img");
        if (!source) return;

        if (!lightbox || !lightbox.showModal) {
          var openedImage = window.open(source, "_blank");
          if (openedImage) openedImage.opener = null;
          return;
        }

        if (lightboxImage) {
          lightboxImage.src = source;
          lightboxImage.alt = image ? image.alt : "";
        }
        if (lightboxCaption) {
          lightboxCaption.textContent =
            trigger.getAttribute("data-product-caption") || "";
        }
        lightbox.showModal();
      });
    }
  );

  if (lightboxClose && lightbox) {
    lightboxClose.addEventListener("click", function () {
      lightbox.close();
    });
  }
  if (lightbox) {
    lightbox.addEventListener("click", function (event) {
      if (event.target === lightbox) lightbox.close();
    });
    lightbox.addEventListener("close", function () {
      if (lightboxImage) lightboxImage.removeAttribute("src");
    });
  }

}());
