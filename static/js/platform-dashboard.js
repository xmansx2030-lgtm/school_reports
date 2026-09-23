(function () {
  "use strict";

  const root = document.querySelector("[data-platform-dashboard]");
  if (!root) return;

  const numberFormatter = new Intl.NumberFormat("ar-SA");
  const moneyFormatter = new Intl.NumberFormat("ar-SA", {
    maximumFractionDigits: 0,
  });
  const charts = new Map();

  function readJsonScript(id) {
    const node = document.getElementById(id);
    if (!node) return {};
    try {
      const value = JSON.parse(node.textContent || "{}");
      return typeof value === "string" ? JSON.parse(value) : value;
    } catch (_error) {
      return {};
    }
  }

  function cssToken(name, fallbackName) {
    const styles = getComputedStyle(root);
    return styles.getPropertyValue(name).trim()
      || styles.getPropertyValue(fallbackName || "--twq-text").trim();
  }

  function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function asNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function formatNumber(value) {
    return numberFormatter.format(asNumber(value));
  }

  function formatBytes(value) {
    let bytes = Math.max(0, asNumber(value));
    const units = ["بايت", "ك.ب", "م.ب", "ج.ب", "ت.ب"];
    let unitIndex = 0;
    while (bytes >= 1024 && unitIndex < units.length - 1) {
      bytes /= 1024;
      unitIndex += 1;
    }
    return `${numberFormatter.format(Math.round(bytes * 10) / 10)} ${units[unitIndex]}`;
  }

  function setStatus(message, state) {
    const status = document.getElementById("dashboardStatus");
    if (!status) return;
    status.textContent = message || "";
    status.classList.remove("is-success", "is-error");
    if (state) status.classList.add(`is-${state}`);
  }

  function setAttentionState(id, count, stateClass) {
    const value = document.getElementById(id);
    if (!value) return;
    value.textContent = formatNumber(count);
    const card = value.closest(".platform-dashboard__attention");
    if (!card) return;
    card.classList.toggle(stateClass, asNumber(count) > 0);
  }

  function chartHasData(values) {
    return Array.isArray(values) && values.some((value) => asNumber(value) !== 0);
  }

  function toggleChartState(canvasId, values) {
    const canvas = document.getElementById(canvasId);
    const empty = document.querySelector(`[data-chart-empty="${canvasId}"]`);
    const hasData = chartHasData(values);
    if (canvas) canvas.hidden = !hasData;
    if (empty) empty.hidden = hasData;
    return hasData;
  }

  function replaceRows(tbodyId, labels, values, kind) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    const fragment = document.createDocumentFragment();
    const safeLabels = Array.isArray(labels) ? labels : [];
    const safeValues = Array.isArray(values) ? values : [];

    if (!safeLabels.length) {
      const row = document.createElement("tr");
      row.dataset.emptyRow = "";
      const cell = document.createElement("td");
      cell.colSpan = 2;
      cell.textContent = "لا توجد بيانات.";
      row.appendChild(cell);
      fragment.appendChild(row);
    } else {
      safeLabels.forEach((label, index) => {
        const row = document.createElement("tr");
        const labelCell = document.createElement("td");
        const valueCell = document.createElement("td");
        labelCell.dir = "ltr";
        valueCell.dir = "ltr";
        labelCell.textContent = String(label);
        valueCell.textContent = kind === "money"
          ? new Intl.NumberFormat("ar-SA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(asNumber(safeValues[index]))
          : formatNumber(safeValues[index]);
        row.append(labelCell, valueCell);
        fragment.appendChild(row);
      });
    }
    tbody.replaceChildren(fragment);
  }

  function chartOptions(kind) {
    const text = cssToken("--twq-text-secondary");
    const grid = cssToken("--twq-divider", "--twq-border");
    const options = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      locale: "ar-SA",
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: kind === "doughnut",
          position: "bottom",
          rtl: true,
          labels: { color: text, usePointStyle: true },
        },
        tooltip: {
          rtl: true,
          titleAlign: "right",
          bodyAlign: "right",
        },
      },
    };
    if (kind !== "doughnut") {
      options.scales = {
        x: { ticks: { color: text }, grid: { color: grid } },
        y: { beginAtZero: true, ticks: { color: text }, grid: { color: grid } },
      };
    }
    return options;
  }

  function createOrUpdateChart(canvasId, type, labels, values, label, colors) {
    const safeLabels = Array.isArray(labels) ? labels : [];
    const safeValues = Array.isArray(values) ? values.map(asNumber) : [];
    if (!toggleChartState(canvasId, safeValues)) {
      const existing = charts.get(canvasId);
      if (existing) existing.destroy();
      charts.delete(canvasId);
      return;
    }

    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof window.Chart === "undefined") {
      if (canvas) canvas.hidden = true;
      const empty = document.querySelector(`[data-chart-empty="${canvasId}"]`);
      if (empty) {
        empty.hidden = false;
        empty.textContent = "تعذر تحميل الرسم؛ البيانات الرقمية متاحة أدناه.";
      }
      return;
    }

    const existing = charts.get(canvasId);
    if (existing) existing.destroy();

    const primary = cssToken("--twq-primary");
    const primarySoft = cssToken("--twq-primary-soft");
    const chart = new window.Chart(canvas, {
      type,
      data: {
        labels: safeLabels,
        datasets: [{
          label,
          data: safeValues,
          borderColor: Array.isArray(colors) ? undefined : primary,
          backgroundColor: Array.isArray(colors) ? colors : primarySoft,
          pointBackgroundColor: primary,
          borderWidth: 2,
          fill: type === "line",
          tension: type === "line" ? 0.28 : 0,
        }],
      },
      options: chartOptions(type === "doughnut" ? "doughnut" : "axis"),
    });
    charts.set(canvasId, chart);
  }

  function stageColors(count) {
    const palette = [
      cssToken("--twq-primary"),
      cssToken("--twq-info"),
      cssToken("--twq-success"),
      cssToken("--twq-warning"),
      cssToken("--twq-neutral"),
    ];
    return Array.from({ length: count }, (_item, index) => palette[index % palette.length]);
  }

  function renderPeriodPayload(payload) {
    const kpis = payload.kpis || {};
    const operations = payload.operations || {};
    const revenue = (payload.charts || {}).revenue || {};
    const reports = (payload.charts || {}).reports || {};

    setText("dashboardGeneratedAt", payload.generated_at || "—");
    setText("dashboardPeriodLabel", `الفترة: ${payload.period_label || "كل الوقت"}`);
    setText("schoolsTotalValue", formatNumber(kpis.schools_total));
    setText("schoolsActiveValue", formatNumber(kpis.schools_active));
    setText("subscriptionsActiveValue", formatNumber(kpis.subscriptions_active));
    setText("totalRevenueValue", `${moneyFormatter.format(asNumber(kpis.total_revenue))} ر.س`);
    setText("ticketsTotalValue", formatNumber(kpis.tickets_total));
    setText("ticketsDoneValue", formatNumber(kpis.tickets_done));
    setText("reportsCountValue", formatNumber(kpis.reports_count));
    setText("storageUsedValue", formatBytes(kpis.storage_used_bytes));
    setText("storageNearLimitValue", formatNumber(kpis.storage_near_limit));
    setText("ticketsOpenSummary", formatNumber(kpis.tickets_open));
    setText("ticketsDoneSummary", formatNumber(kpis.tickets_done));
    setText("ticketsRejectedSummary", formatNumber(kpis.tickets_rejected));

    setAttentionState("pendingPaymentsValue", operations.pending_payments, "is-danger");
    setAttentionState("openTicketsValue", kpis.tickets_open, "is-warning");
    setAttentionState("complaintsPendingValue", operations.complaints_pending, "is-danger");
    setAttentionState("expiringSubscriptionsValue", operations.subscriptions_expiring_soon, "is-info");

    replaceRows("revenueChartRows", revenue.labels, revenue.data, "money");
    replaceRows("reportsChartRows", reports.labels, reports.data, "number");
    createOrUpdateChart("revenueChart", "line", revenue.labels, revenue.data, "الإيرادات المعتمدة", null);
    createOrUpdateChart("reportsChart", "bar", reports.labels, reports.data, "التقارير المنشأة", null);

    root.querySelectorAll("[data-period]").forEach((button) => {
      const active = button.dataset.period === (payload.period || "all");
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  async function loadPeriod(period, force) {
    const apiUrl = root.dataset.apiUrl;
    if (!apiUrl) return;
    const url = new URL(apiUrl, window.location.origin);
    url.searchParams.set("period", period || "all");
    if (force) url.searchParams.set("refresh", "1");
    setStatus(force ? "جارٍ جلب أحدث البيانات من المصدر…" : "جارٍ تحديث الفترة…");

    try {
      const response = await fetch(url, {
        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`Dashboard request failed: ${response.status}`);
      const payload = await response.json();
      renderPeriodPayload(payload);
      setStatus(force ? "تم تحديث مؤشرات المنصة." : "تم تطبيق الفترة المحددة.", "success");
    } catch (_error) {
      setStatus("تعذر تحديث البيانات. تحقق من الاتصال ثم أعد المحاولة.", "error");
    }
  }

  function searchItem(title, subtitle, type, icon, href) {
    return { title, subtitle, type, icon, href };
  }

  function localSearch(query) {
    const normalized = query.trim().toLocaleLowerCase("ar");
    if (normalized.length < 2) return [];
    return Array.from(root.querySelectorAll("[data-search-item]"))
      .filter((node) => (node.dataset.searchTitle || "").toLocaleLowerCase("ar").includes(normalized))
      .slice(0, 4)
      .map((node) => searchItem(
        node.querySelector("strong")?.textContent?.trim() || node.textContent.trim(),
        node.querySelector("small")?.textContent?.trim() || "اختصار تشغيلي",
        "وحدة",
        node.querySelector("i")?.className?.split(" ").find((name) => name.startsWith("fa-")) || "fa-arrow-up-right-from-square",
        node.getAttribute("href") || "#",
      ));
  }

  function renderSearchResults(items) {
    const container = document.getElementById("searchResults");
    const input = document.getElementById("platformDashboardSearch");
    if (!container || !input) return;
    const fragment = document.createDocumentFragment();

    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "platform-dashboard__search-empty";
      empty.textContent = "لا توجد نتائج مطابقة.";
      fragment.appendChild(empty);
    } else {
      items.slice(0, 8).forEach((item) => {
        const link = document.createElement("a");
        const icon = document.createElement("i");
        const copy = document.createElement("span");
        const title = document.createElement("strong");
        const subtitle = document.createElement("small");
        link.className = "platform-dashboard__search-result";
        link.href = item.href;
        link.setAttribute("role", "option");
        icon.className = `fas ${item.icon || "fa-magnifying-glass"}`;
        icon.setAttribute("aria-hidden", "true");
        title.textContent = item.title || "نتيجة";
        subtitle.textContent = [item.type, item.subtitle].filter(Boolean).join(" · ");
        copy.append(title, subtitle);
        link.append(icon, copy);
        fragment.appendChild(link);
      });
    }
    container.replaceChildren(fragment);
    container.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function closeSearch() {
    const container = document.getElementById("searchResults");
    const input = document.getElementById("platformDashboardSearch");
    if (container) container.hidden = true;
    if (input) input.setAttribute("aria-expanded", "false");
  }

  function initSearch() {
    const input = document.getElementById("platformDashboardSearch");
    const container = document.getElementById("searchResults");
    const searchUrl = root.dataset.searchUrl;
    if (!input || !container || !searchUrl) return;
    let timer;
    let requestSequence = 0;

    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      const query = input.value.trim();
      if (query.length < 2) {
        closeSearch();
        return;
      }
      timer = window.setTimeout(async () => {
        const sequence = ++requestSequence;
        const localResults = localSearch(query);
        try {
          const url = new URL(searchUrl, window.location.origin);
          url.searchParams.set("q", query);
          const response = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
          if (!response.ok) throw new Error("Search failed");
          const payload = await response.json();
          if (sequence === requestSequence) renderSearchResults([...localResults, ...(payload.results || [])]);
        } catch (_error) {
          if (sequence === requestSequence) renderSearchResults(localResults);
        }
      }, 240);
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeSearch();
      if (event.key === "ArrowDown" && !container.hidden) {
        const first = container.querySelector("a");
        if (first) {
          event.preventDefault();
          first.focus();
        }
      }
    });

    container.addEventListener("keydown", (event) => {
      const links = Array.from(container.querySelectorAll("a"));
      const index = links.indexOf(document.activeElement);
      if (event.key === "Escape") {
        closeSearch();
        input.focus();
      } else if (event.key === "ArrowDown" && links.length) {
        event.preventDefault();
        links[(index + 1) % links.length].focus();
      } else if (event.key === "ArrowUp" && links.length) {
        event.preventDefault();
        links[(index - 1 + links.length) % links.length].focus();
      }
    });

    document.addEventListener("click", (event) => {
      if (!event.target.closest(".platform-dashboard__search")) closeSearch();
    });
  }

  const initialPayload = readJsonScript("dashboardPeriodPayload");
  const staticCharts = readJsonScript("platformDashboardChartData");
  renderPeriodPayload(initialPayload);

  const stages = staticCharts.stages || {};
  const stageLabels = Array.isArray(stages.labels) ? stages.labels : [];
  const stageValues = Array.isArray(stages.data) ? stages.data : [];
  createOrUpdateChart("schoolsChart", "doughnut", stageLabels, stageValues, "المدارس", stageColors(stageLabels.length));

  root.querySelectorAll("[data-period]").forEach((button) => {
    button.addEventListener("click", () => loadPeriod(button.dataset.period, false));
  });
  root.querySelector("[data-dashboard-refresh]")?.addEventListener("click", () => {
    const active = root.querySelector("[data-period].is-active");
    loadPeriod(active?.dataset.period || initialPayload.period || "all", true);
  });
  root.querySelectorAll("[data-print-chart]").forEach((button) => {
    button.addEventListener("click", () => window.print());
  });
  initSearch();
})();
