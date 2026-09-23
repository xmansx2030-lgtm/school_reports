/* Progressive enhancement for the manager smart brief.
 *
 * The server renders a complete deterministic brief first.  JavaScript only
 * requests an optional AI-polished copy and repaints trusted, server-created
 * actions.  No dashboard metric or permission is calculated in the browser.
 */
(function () {
  "use strict";

  function csrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function text(root, selector, value) {
    var node = root.querySelector(selector);
    if (node) node.textContent = String(value || "");
  }

  function safeHref(value) {
    var href = String(value || "");
    return href.charAt(0) === "/" || href.charAt(0) === "#" ? href : "#";
  }

  function iconFor(key) {
    return {
      coverage: "fa-user-check",
      reports: "fa-file-lines",
      tickets: "fa-inbox"
    }[key] || "fa-chart-simple";
  }

  function renderSignals(root, signals) {
    var list = root.querySelector("[data-brief-signals]");
    if (!list) return;
    list.replaceChildren();
    (signals || []).forEach(function (signal) {
      var link = document.createElement("a");
      link.className = "manager-smart-signal";
      link.href = safeHref(signal.url);
      link.dataset.tone = String(signal.tone || "neutral");

      var icon = document.createElement("span");
      icon.className = "manager-smart-signal__icon";
      icon.setAttribute("aria-hidden", "true");
      var iconGlyph = document.createElement("i");
      iconGlyph.className = "fa-solid " + iconFor(signal.key);
      icon.appendChild(iconGlyph);

      var content = document.createElement("span");
      content.className = "manager-smart-signal__content";
      var label = document.createElement("small");
      label.textContent = String(signal.label || "مؤشر");
      var value = document.createElement("strong");
      value.textContent = String(signal.value || "—");
      var context = document.createElement("span");
      context.textContent = String(signal.context || "");
      content.append(label, value, context);

      var arrow = document.createElement("i");
      arrow.className = "fa-solid fa-arrow-left manager-smart-signal__arrow";
      arrow.setAttribute("aria-hidden", "true");
      link.append(icon, content, arrow);
      list.appendChild(link);
    });
  }

  function renderPriorities(root, priorities) {
    var list = root.querySelector("[data-brief-priorities]");
    if (!list) return;
    list.replaceChildren();
    (priorities || []).forEach(function (item, index) {
      var row = document.createElement("li");
      row.className = "manager-smart-priority";
      row.dataset.tone = String(item.tone || "neutral");

      var rank = document.createElement("span");
      rank.className = "manager-smart-priority__rank";
      rank.textContent = String(item.rank || index + 1);
      rank.setAttribute("aria-label", "الأولوية " + rank.textContent);

      var content = document.createElement("span");
      content.className = "manager-smart-priority__content";
      var title = document.createElement("strong");
      title.textContent = String(item.title || "أولوية تشغيلية");
      var description = document.createElement("span");
      description.textContent = String(item.description || "");
      content.append(title, description);

      var action = document.createElement("a");
      action.className = "manager-smart-priority__action";
      action.href = safeHref(item.url);
      action.append(document.createTextNode(String(item.action_label || "فتح")));
      var arrow = document.createElement("i");
      arrow.className = "fa-solid fa-arrow-left";
      arrow.setAttribute("aria-hidden", "true");
      action.append(document.createTextNode(" "), arrow);

      row.append(rank, content, action);
      list.appendChild(row);
    });
    text(root, "[data-brief-priority-count]", (priorities || []).length);
  }

  function paint(root, brief) {
    root.dataset.period = String(brief.period || "all");
    root.dataset.generatedAt = String(brief.generated_at || "");
    root.dataset.status = String(brief.status || "neutral");
    root.classList.remove("is-stale");
    text(root, "[data-brief-period]", brief.period_label);
    text(root, "[data-brief-source]", brief.source_label);
    var source = root.querySelector("[data-brief-source]");
    if (source) {
      var shield = document.createElement("i");
      shield.className = "fa-solid fa-shield-halved";
      shield.setAttribute("aria-hidden", "true");
      source.prepend(shield);
    }
    text(root, "[data-brief-headline]", brief.headline);
    text(root, "[data-brief-summary]", brief.summary);
    text(root, "[data-brief-generated]", brief.generated_at || "الآن");
    renderSignals(root, brief.signals);
    renderPriorities(root, brief.priorities);
  }

  function setup(root) {
    var endpoint = root.dataset.endpoint || "";
    var button = root.querySelector("[data-brief-refresh]");
    var status = root.querySelector("[data-brief-status]");
    var buttonLabel = button ? button.querySelector("span") : null;
    var normalLabel = buttonLabel ? buttonLabel.textContent : "تحديث الموجز";
    var controller = null;

    function announce(message, error) {
      if (!status) return;
      status.textContent = message || "";
      status.classList.toggle("is-error", !!error);
    }

    function setLoading(loading) {
      root.setAttribute("aria-busy", loading ? "true" : "false");
      if (button) button.disabled = loading;
      if (buttonLabel) buttonLabel.textContent = loading ? "جارٍ قراءة المؤشرات…" : normalLabel;
      var icon = button ? button.querySelector("i") : null;
      if (icon) icon.className = loading
        ? "fa-solid fa-spinner fa-spin"
        : "fa-solid fa-sparkles";
    }

    async function refresh() {
      if (!endpoint || !button || button.disabled) return;
      if (controller) controller.abort();
      controller = typeof AbortController === "function" ? new AbortController() : null;
      setLoading(true);
      announce("أقرأ مؤشرات الفترة وأرتب الأولويات…", false);
      try {
        var response = await fetch(endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken()
          },
          body: JSON.stringify({period: root.dataset.period || "all"}),
          signal: controller ? controller.signal : undefined
        });
        var data = await response.json().catch(function () { return {}; });
        if (!response.ok || !data.ok || !data.brief) {
          throw new Error(data.message || "تعذر تحديث الموجز الآن.");
        }
        paint(root, data.brief);
        announce(
          data.brief.ai_generated
            ? "تم تحديث الصياغة الذكية. راجع الأولويات ثم افتح الإجراء المناسب."
            : "تم تحديث الموجز من بيانات النظام الموثوقة.",
          false
        );
      } catch (error) {
        if (error && error.name === "AbortError") return;
        announce(error && error.message ? error.message : "تعذر تحديث الموجز الآن.", true);
      } finally {
        setLoading(false);
      }
    }

    if (button) button.addEventListener("click", refresh);
    window.addEventListener("tawtheeq:manager-dashboard-updated", function (event) {
      var detail = event.detail || {};
      var nextPeriod = String(detail.period || "all");
      var nextGenerated = String(detail.generatedAt || "");
      var changed = nextPeriod !== String(root.dataset.period || "all") ||
        (nextGenerated && nextGenerated !== String(root.dataset.generatedAt || ""));
      root.dataset.period = nextPeriod;
      if (!changed) return;
      root.classList.add("is-stale");
      text(root, "[data-brief-period]", "الفترة المحدّثة");
      announce("تغيّرت بيانات اللوحة. حدّث الموجز ليقرأ الفترة الجديدة.", false);
    });
  }

  var root = document.getElementById("managerBrief");
  if (root) setup(root);
}());
