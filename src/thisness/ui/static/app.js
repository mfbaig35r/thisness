// Thisness Workbench: the side panel, the command menu, and the review board.

(function () {
  "use strict";

  // -- side panel: any [hx-target="#panel-body"] swap opens it -------------------------------
  const panel = () => document.getElementById("panel");
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target.id === "panel-body") panel().classList.add("open");
    if (e.detail.target.closest && e.detail.target.closest(".board-wrap")) initBoard();
  });
  window.closePanel = () => panel() && panel().classList.remove("open");

  // -- command menu ---------------------------------------------------------------------------
  const menu = () => document.getElementById("cmdk");
  window.openMenu = () => {
    const m = menu();
    if (!m.open) m.showModal();
    const input = m.querySelector("input");
    input.select();
  };
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      window.openMenu();
      return;
    }
    if (e.key === "Escape" && !(menu() && menu().open)) window.closePanel();
    const m = menu();
    if (m && m.open && (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter")) {
      const hits = [...m.querySelectorAll(".hit")];
      if (!hits.length) return;
      let i = hits.findIndex((h) => h.classList.contains("selected"));
      if (e.key === "Enter") {
        e.preventDefault();
        (hits[i] || hits[0]).click();
        return;
      }
      e.preventDefault();
      i = e.key === "ArrowDown" ? Math.min(i + 1, hits.length - 1) : Math.max(i - 1, 0);
      hits.forEach((h, j) => h.classList.toggle("selected", j === i));
      hits[i].scrollIntoView({ block: "nearest" });
    }
  });

  // -- review board ---------------------------------------------------------------------------
  function groups() {
    return [...document.querySelectorAll(".column-body")]
      .map((col) => [...col.querySelectorAll(".node-card")].map((c) => c.dataset.node))
      .filter((g) => g.length);
  }

  function changed() {
    const cards = [...document.querySelectorAll(".node-card")];
    let moved = false;
    cards.forEach((c) => {
      const here = c.closest(".column").dataset.column;
      c.classList.toggle("moved", here !== c.dataset.home);
      moved = moved || here !== c.dataset.home;
    });
    const save = document.getElementById("save-groups");
    if (save) save.disabled = !moved;
    const field = document.getElementById("groups-field");
    if (field) field.value = JSON.stringify(groups());
  }

  function initBoard() {
    if (typeof Sortable === "undefined") return;
    document.querySelectorAll(".column-body").forEach((el) => {
      if (el.dataset.sortable) return;
      el.dataset.sortable = "1";
      Sortable.create(el, { group: "nodes", animation: 120, onSort: changed });
    });
    changed();
  }
  window.applyCut = (keep, move) => {
    const target = document.querySelector(".column.new .column-body");
    move.forEach((id) => {
      const card = document.querySelector(`.node-card[data-node="${id}"]`);
      if (card && target) target.appendChild(card);
    });
    changed();
  };
  // A suggestion's grouping onto the board: each group goes to the column that already
  // holds most of it, or to a new column.
  window.applyGroups = (groups, resolution) => {
    const board = document.querySelector(".board");
    const template = document.querySelector(".column.new");
    const used = new Set();
    groups.forEach((group) => {
      const counts = {};
      group.forEach((id) => {
        const card = document.querySelector(`.node-card[data-node="${id}"]`);
        const col = card && card.closest(".column");
        if (col && !col.classList.contains("new")) counts[col.dataset.column] = (counts[col.dataset.column] || 0) + 1;
      });
      const best = Object.keys(counts).filter((c) => !used.has(c)).sort((a, b) => counts[b] - counts[a])[0];
      let body;
      if (best) {
        used.add(best);
        body = document.querySelector(`.column[data-column="${best}"] .column-body`);
      } else {
        const fresh = template.cloneNode(true);
        fresh.dataset.column = "new-" + used.size;
        fresh.querySelector(".column-body").innerHTML = "";
        delete fresh.querySelector(".column-body").dataset.sortable;
        board.insertBefore(fresh, template);
        used.add(fresh.dataset.column);
        body = fresh.querySelector(".column-body");
      }
      group.forEach((id) => {
        const card = document.querySelector(`.node-card[data-node="${id}"]`);
        if (card) body.appendChild(card);
      });
    });
    initBoard();
    changed();
    const form = document.querySelector("form.answer");
    if (form && resolution) {
      const target = { same: "same", different: "different", related: "related" }[resolution];
      form.querySelectorAll("button").forEach((b) => b.classList.remove("primary"));
      const pick = target
        ? [...form.querySelectorAll("button")].find((b) => (b.getAttribute("onclick") || "").includes(`'${target}'`))
        : document.getElementById("save-groups");
      if (pick) pick.classList.add("primary");
    }
  };
  window.setResolution = (form, value) => {
    form.querySelector("[name=resolution]").value = value;
    const field = document.getElementById("groups-field");
    if (field) field.value = JSON.stringify(groups());
  };
  document.addEventListener("DOMContentLoaded", initBoard);
  document.addEventListener("htmx:load", initBoard);
})();
