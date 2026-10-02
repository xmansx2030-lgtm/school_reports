(function () {
  "use strict";
  if (!window.MutationObserver) return;
  [["improvement", "[data-report-ai-improver]"], ["voice", "[data-report-voice]"]].forEach(function (tool) {
    var editor = document.querySelector(tool[1]);
    var summary = document.querySelector('[data-assistant-kind="' + tool[0] + '"]');
    if (!editor || !summary || !summary.querySelector("[data-assistant-remaining]")) return;
    function sync() {
      var remaining = Number(editor.getAttribute("data-remaining"));
      if (!Number.isFinite(remaining) || remaining < 0) return;
      summary.querySelector("[data-assistant-remaining]").textContent = String(remaining);
      var exhausted = remaining === 0;
      summary.setAttribute("data-assistant-state", exhausted ? "exhausted" : "available");
      var label = summary.querySelector("[data-assistant-label]");
      var availableLabel = tool[0] === "voice" && editor.getAttribute("data-pwa-only") === "1"
        ? "متاح في التطبيق المثبّت" : "متاح الآن";
      label.textContent = exhausted ? "اكتمل استخدام اليوم" : availableLabel;
      label.classList.toggle("twq-status--success", !exhausted);
      label.classList.toggle("twq-status--warning", exhausted);
    }
    sync();
    new MutationObserver(sync).observe(editor, { attributes: true, attributeFilter: ["data-remaining"] });
  });
}());
