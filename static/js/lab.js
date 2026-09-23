(() => {
  "use strict";

  const focusErrorSummary = () => {
    const summary = document.querySelector("[data-lab-error-summary]");
    if (!(summary instanceof HTMLElement)) return;

    window.requestAnimationFrame(() => {
      summary.focus({ preventScroll: true });
      summary.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", focusErrorSummary, { once: true });
  } else {
    focusErrorSummary();
  }
})();
