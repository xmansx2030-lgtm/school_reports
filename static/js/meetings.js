(() => {
  "use strict";

  const focusErrorSummary = () => {
    const summary = document.querySelector("[data-meeting-error-summary]");
    if (!(summary instanceof HTMLElement)) return;
    window.requestAnimationFrame(() => {
      summary.focus({ preventScroll: true });
      summary.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  };

  const initAttendeePicker = () => {
    const list = document.querySelector("[data-attendee-list]");
    if (!(list instanceof HTMLElement)) return;

    const search = document.querySelector("[data-attendee-search]");
    const noResult = document.querySelector("[data-attendee-no-result]");
    const counter = document.querySelector("[data-attendee-count]");
    const hint = document.querySelector("[data-attendee-hint]");
    const selectAll = document.querySelector("[data-attendee-select-all]");
    const clear = document.querySelector("[data-attendee-clear]");
    const tiles = Array.from(list.querySelectorAll("[data-attendee-option]"));

    const checkboxOf = (tile) => tile.querySelector('input[type="checkbox"]');
    const refresh = () => {
      const chosen = tiles.filter((tile) => checkboxOf(tile)?.checked).length;
      if (counter) counter.textContent = String(chosen);
      if (hint) {
        hint.textContent = chosen
          ? `ستصل الدعوة إلى ${chosen} من منسوبي المدرسة.`
          : "اختر مدعوًا واحدًا على الأقل.";
      }
    };

    search?.addEventListener("input", () => {
      const term = search.value.trim();
      let shown = 0;
      tiles.forEach((tile) => {
        const match = !term || (tile.dataset.name || "").includes(term);
        tile.hidden = !match;
        if (match) shown += 1;
      });
      noResult?.classList.toggle("is-visible", shown === 0);
    });

    list.addEventListener("change", refresh);
    selectAll?.addEventListener("click", () => {
      tiles.filter((tile) => !tile.hidden).forEach((tile) => {
        const checkbox = checkboxOf(tile);
        if (checkbox) checkbox.checked = true;
      });
      refresh();
    });
    clear?.addEventListener("click", () => {
      tiles.forEach((tile) => {
        const checkbox = checkboxOf(tile);
        if (checkbox) checkbox.checked = false;
      });
      refresh();
    });
    refresh();
  };

  const notify = (message) => {
    if (window.showAppToast) window.showAppToast(message, "success");
    else if (window.rcAlert) window.rcAlert(message, { type: "info" });
  };

  const copyText = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const field = document.createElement("textarea");
    field.className = "meeting-copy-fallback";
    field.value = text;
    field.setAttribute("readonly", "");
    document.body.appendChild(field);
    field.select();
    try {
      document.execCommand("copy");
    } finally {
      field.remove();
    }
  };

  const initCopyLinks = () => {
    document.querySelectorAll("[data-copy-link]").forEach((trigger) => {
      trigger.addEventListener("click", async () => {
        const url = trigger.getAttribute("data-copy-link") || "";
        const title = trigger.getAttribute("data-share-title") || document.title;
        if (!url) return;
        if (navigator.share && trigger.hasAttribute("data-share-native")) {
          try {
            await navigator.share({ title, url });
            return;
          } catch (error) {
            if (error?.name === "AbortError") return;
          }
        }
        try {
          await copyText(url);
          notify("نُسخ رابط الاجتماع — يفتح لمن دُعي إليه فقط.");
        } catch (_error) {
          if (window.rcPrompt) {
            await window.rcPrompt("تعذر النسخ التلقائي. انسخ الرابط يدويًا:", url, {
              required: false,
              title: "نسخ رابط الاجتماع",
              okText: "إغلاق",
            });
          }
        }
      });
    });
  };

  const initMinutesFormat = () => {
    const picker = document.querySelector("[data-minutes-format]");
    if (!picker) return;
    const sync = () => {
      const selected = picker.querySelector("input:checked");
      const mode = selected?.value || "freeform";
      document.querySelectorAll("[data-minutes-panel]").forEach((panel) => {
        panel.hidden = panel.getAttribute("data-minutes-panel") !== mode;
      });
    };
    picker.addEventListener("change", sync);
    sync();
  };

  const initApprovalActions = () => {
    const field = document.querySelector("[data-meeting-approval-action]");
    if (!(field instanceof HTMLInputElement)) return;
    document.querySelectorAll("[data-meeting-approval-button]").forEach((button) => {
      button.addEventListener("click", () => {
        field.value = button.getAttribute("data-meeting-approval-button") || "";
      });
    });
  };

  const initCancelDialog = () => {
    const dialog = document.querySelector("[data-meeting-cancel-dialog]");
    const open = document.querySelector("[data-meeting-cancel-open]");
    const close = document.querySelector("[data-meeting-cancel-close]");
    if (!(dialog instanceof HTMLDialogElement) || !(open instanceof HTMLElement)) return;
    open.addEventListener("click", () => {
      dialog.showModal();
      dialog.querySelector("input")?.focus();
    });
    close?.addEventListener("click", () => dialog.close());
  };

  const init = () => {
    focusErrorSummary();
    initAttendeePicker();
    initCopyLinks();
    initMinutesFormat();
    initApprovalActions();
    initCancelDialog();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
