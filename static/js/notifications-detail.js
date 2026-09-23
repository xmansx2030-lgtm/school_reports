(function () {
  "use strict";

  const root = document.querySelector(".notifications-sender-detail");
  const table = document.getElementById("recTable");
  if (!root || !table) return;

  const search = document.getElementById("recSearch");
  const buttons = Array.from(root.querySelectorAll("[data-recipient-filter]"));
  const rows = Array.from(table.querySelectorAll("[data-recipient-row]"));
  const empty = document.getElementById("recipientFilterEmpty");
  let activeFilter = "all";

  function applyFilter() {
    const query = (search ? search.value : "").trim().toLocaleLowerCase("ar");
    let visible = 0;
    rows.forEach((row) => {
      const matchesText = !query || (row.dataset.search || row.textContent).toLocaleLowerCase("ar").includes(query);
      const matchesState = activeFilter === "all"
        || (activeFilter === "unread" && row.dataset.read !== "1")
        || (activeFilter === "unsigned" && row.dataset.signed !== "1");
      row.hidden = !(matchesText && matchesState);
      if (!row.hidden) visible += 1;
    });
    if (empty) empty.hidden = visible !== 0;
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.recipientFilter || "all";
      buttons.forEach((candidate) => {
        const selected = candidate === button;
        candidate.classList.toggle("is-active", selected);
        candidate.setAttribute("aria-pressed", String(selected));
      });
      applyFilter();
    });
  });
  if (search) search.addEventListener("input", applyFilter);
})();
