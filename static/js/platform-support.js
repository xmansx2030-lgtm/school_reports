(() => {
  "use strict";
  const banner = document.querySelector("[data-support-context]");
  if (!banner) return;
  const context = banner.dataset.supportContext;
  const isLocal = (url) => new URL(url, location.href).origin === location.origin;
  const protect = (form) => {
    if (!(form instanceof HTMLFormElement)) return;
    if (form.method.toLowerCase() === "get" || !isLocal(form.action)) return;
    let field = form.querySelector('input[name="_support_context"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = "_support_context";
      form.append(field);
    }
    field.value = context;
  };
  document.querySelectorAll("form").forEach(protect);
  document.addEventListener("submit", (event) => protect(event.target), true);
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = input instanceof Request ? input.url : String(input);
    const method = (init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (isLocal(url) && !["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("X-Platform-Support-Context", context);
      return originalFetch(input, { ...init, headers });
    }
    return originalFetch(input, init);
  };
})();
