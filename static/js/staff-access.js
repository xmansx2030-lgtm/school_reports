(function () {
  "use strict";

  function byId(id) {
    return id ? document.getElementById(id) : null;
  }

  function focusError(root) {
    var summary = root.querySelector("[data-error-summary]");
    if (summary) summary.focus();
  }

  function initTabs(root) {
    var tabs = Array.prototype.slice.call(root.querySelectorAll(".rol-tab"));
    if (!tabs.length) return function () {};

    function show(name, focusTab) {
      tabs.forEach(function (tab) {
        var selected = tab.getAttribute("data-tab") === name;
        tab.setAttribute("aria-selected", selected ? "true" : "false");
        tab.setAttribute("tabindex", selected ? "0" : "-1");
        var panel = byId("panel-" + tab.getAttribute("data-tab"));
        if (panel) panel.hidden = !selected;
        if (selected && focusTab) tab.focus();
      });
      try {
        window.history.replaceState(null, "", "#" + name);
      } catch (error) {
        // Hash persistence is progressive enhancement only.
      }
    }

    tabs.forEach(function (tab, index) {
      tab.addEventListener("click", function () {
        show(tab.getAttribute("data-tab"), false);
      });
      tab.addEventListener("keydown", function (event) {
        var next = index;
        if (event.key === "ArrowLeft") next = (index + 1) % tabs.length;
        else if (event.key === "ArrowRight") next = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = tabs.length - 1;
        else return;
        event.preventDefault();
        show(tabs[next].getAttribute("data-tab"), true);
      });
    });

    var fromHash = (window.location.hash || "").replace("#", "");
    var allowed = tabs.map(function (tab) { return tab.getAttribute("data-tab"); });
    var initial = allowed.indexOf(fromHash) >= 0
      ? fromHash
      : root.getAttribute("data-open-tab") || allowed[0];
    show(initial, false);
    return show;
  }

  function initRoleAssignment(root, showTab) {
    var roleInputs = Array.prototype.slice.call(
      root.querySelectorAll('#panel-assign input[name="role_type"]')
    );
    var labPanel = byId("assignLabKindPanel");
    var labSelect = byId(root.getAttribute("data-lab-select"));

    function syncLabKind() {
      var selected = roleInputs.find(function (input) { return input.checked; });
      var isLab = Boolean(selected && selected.value === "lab_tech");
      if (labPanel) {
        labPanel.hidden = !isLab;
        labPanel.setAttribute("aria-hidden", isLab ? "false" : "true");
      }
      if (labSelect) labSelect.required = isLab;
    }

    roleInputs.forEach(function (input) {
      input.addEventListener("change", syncLabKind);
    });
    syncLabKind();

    function pick(selectId, personId, tabName) {
      var select = byId(selectId);
      if (select) {
        select.value = String(personId);
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      showTab(tabName, true);
      var panel = byId("panel-" + tabName);
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    root.querySelectorAll("[data-assign]").forEach(function (button) {
      button.addEventListener("click", function () {
        pick(
          root.getAttribute("data-assign-select"),
          button.getAttribute("data-assign"),
          "assign"
        );
      });
    });

    root.querySelectorAll("[data-delegate]").forEach(function (button) {
      button.addEventListener("click", function () {
        pick(
          root.getAttribute("data-delegate-select"),
          button.getAttribute("data-delegate"),
          "delegate"
        );
      });
    });
  }

  function initSelectFilters(root) {
    root.querySelectorAll(".rol-pick").forEach(function (filter) {
      var select = byId(filter.getAttribute("data-picks"));
      if (!select) return;
      var options = Array.prototype.slice.call(select.options).map(function (option) {
        return { option: option, text: (option.textContent || "").toLowerCase() };
      });

      filter.addEventListener("input", function () {
        var needle = filter.value.trim().toLowerCase();
        var visibleOptions = [];
        options.forEach(function (item) {
          var visible = !needle || item.text.indexOf(needle) >= 0 || !item.option.value;
          item.option.hidden = !visible;
          if (visible && item.option.value) visibleOptions.push(item.option);
        });
        if (visibleOptions.length === 1) {
          select.value = visibleOptions[0].value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    });
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function stamp(date) {
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
      + "T" + pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  function readDate(field) {
    if (!field || !field.value) return null;
    var parsed = new Date(field.value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function initDelegation(root) {
    var startField = byId(root.getAttribute("data-start-field"));
    var endField = byId(root.getAttribute("data-end-field"));
    var delegateField = byId(root.getAttribute("data-delegate-select"));
    var summary = byId("delegationSummary");
    var count = byId("capsCount");
    var checks = Array.prototype.slice.call(root.querySelectorAll(".rol-cap__input"));

    function describe() {
      if (!summary) return;
      var name = "";
      if (delegateField && delegateField.selectedIndex >= 0) {
        var selected = delegateField.options[delegateField.selectedIndex];
        if (selected && selected.value) name = selected.textContent.trim();
      }
      var chosen = checks.filter(function (check) { return check.checked; }).length;
      if (count) count.textContent = chosen ? "اختيرت " + chosen + " صلاحية" : "لم تُختر صلاحيات";

      var startsAt = readDate(startField);
      var endsAt = readDate(endField);
      if (!name || !startsAt || !endsAt || !chosen) {
        summary.textContent = "اختر المفوَّض إليه والمدة والصلاحيات ليظهر ملخّص التفويض هنا.";
        return;
      }
      if (endsAt <= startsAt) {
        summary.textContent = "نهاية التفويض يجب أن تكون بعد بدايته.";
        return;
      }
      var days = Math.max(1, Math.round((endsAt - startsAt) / 86400000));
      summary.textContent = "سيمارس " + name + " " + chosen
        + " من صلاحيات المدير بالنيابة لمدة " + days
        + " يومًا. لا يضيف التفويض أي قسم إلى نطاقه، وينتهي تلقائيًا.";
    }

    root.querySelectorAll("[data-days]").forEach(function (button) {
      button.addEventListener("click", function () {
        var days = parseInt(button.getAttribute("data-days"), 10) || 1;
        var startsAt = readDate(startField) || new Date();
        if (startField && !startField.value) startField.value = stamp(startsAt);
        if (endField) endField.value = stamp(new Date(startsAt.getTime() + days * 86400000));
        root.querySelectorAll("[data-days]").forEach(function (other) {
          other.setAttribute("aria-pressed", other === button ? "true" : "false");
        });
        describe();
      });
    });

    root.querySelectorAll("[data-preset]").forEach(function (button) {
      button.addEventListener("click", function () {
        var wanted = (button.getAttribute("data-preset") || "").split(",").filter(Boolean);
        checks.forEach(function (check) { check.checked = wanted.indexOf(check.value) >= 0; });
        describe();
      });
    });

    var clear = byId("capsNone");
    if (clear) {
      clear.addEventListener("click", function () {
        checks.forEach(function (check) { check.checked = false; });
        describe();
      });
    }

    checks.forEach(function (check) { check.addEventListener("change", describe); });
    [startField, endField, delegateField].forEach(function (field) {
      if (field) field.addEventListener("change", describe);
    });
    describe();
  }

  function initScope(root) {
    var checks = Array.prototype.slice.call(root.querySelectorAll('input[name="capabilities"]'));
    var allowed = byId("scopeAllowedList");
    var denied = byId("scopeDeniedList");
    var empty = byId("scopeAllowedEmpty");
    if (!checks.length || !allowed || !denied) return;

    function refresh() {
      var selected = 0;
      checks.forEach(function (check) {
        var allowedItem = allowed.querySelector('[data-capability="' + check.value + '"]');
        var deniedItem = denied.querySelector('[data-capability="' + check.value + '"]');
        if (allowedItem) allowedItem.hidden = !check.checked;
        if (deniedItem) deniedItem.hidden = check.checked;
        if (check.checked) selected += 1;
      });
      if (empty) empty.hidden = selected > 0;
    }

    checks.forEach(function (check) { check.addEventListener("change", refresh); });
    refresh();
  }

  var accessRoot = document.querySelector("[data-staff-access]");
  if (accessRoot) {
    var showTab = initTabs(accessRoot);
    initRoleAssignment(accessRoot, showTab);
    initSelectFilters(accessRoot);
    initDelegation(accessRoot);
    focusError(accessRoot);
  }

  var scopeRoot = document.querySelector("[data-staff-scope]");
  if (scopeRoot) {
    initScope(scopeRoot);
    focusError(scopeRoot);
  }
}());
