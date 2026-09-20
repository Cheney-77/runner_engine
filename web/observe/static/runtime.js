"use strict";

(() => {
  const sandboxView = document.getElementById("sandboxView");
  if (!sandboxView) return;

  function createCard(label, foot) {
    const card = document.createElement("article");
    card.className = "kpi-card";

    const caption = document.createElement("span");
    caption.className = "kpi-label";
    caption.textContent = label;

    const value = document.createElement("div");
    value.className = "kpi-value";
    value.textContent = "—";

    const hint = document.createElement("span");
    hint.className = "kpi-foot";
    hint.textContent = foot;

    card.append(caption, value, hint);
    return {card, value};
  }

  const active = createCard("ACTIVE INVOCATIONS", "Runner 当前 one-shot child 调用");
  const idle = createCard("IDLE WARM WORKERS", "WorkerPool 中可复用 sandbox");
  const retiring = createCard("RETIRING RUNTIMES", "已关闭新入口，等待排空");
  const retired = createCard("RETIRED RUNTIMES", "已从 Runner 执行面永久阻断");

  const grid = document.createElement("div");
  grid.className = "kpi-grid";
  grid.append(active.card, idle.card, retiring.card, retired.card);

  const firstGrid = sandboxView.querySelector(".kpi-grid");
  if (firstGrid) firstGrid.insertAdjacentElement("afterend", grid);
  else sandboxView.prepend(grid);

  function count(items, state) {
    return Array.isArray(items)
      ? items.filter((item) => item.state === state).length
      : 0;
  }

  async function refresh() {
    try {
      const response = await fetch("/observe/api/runner-runtime", {
        cache: "no-store",
        credentials: "same-origin",
        headers: {Accept: "application/json"},
      });
      const payload = await response.json();

      if (!response.ok || payload.status !== "ok") {
        throw new Error(payload.message || `HTTP ${response.status}`);
      }

      active.value.textContent = String(payload.activeInvocationCount ?? 0);
      idle.value.textContent = String(payload.idleWorkerCount ?? 0);
      retiring.value.textContent = String(
        count(payload.runtimeLifecycle, "RETIRING"),
      );
      retired.value.textContent = String(
        count(payload.runtimeLifecycle, "RETIRED"),
      );
    } catch {
      active.value.textContent = "—";
      idle.value.textContent = "—";
      retiring.value.textContent = "—";
      retired.value.textContent = "—";
    }
  }

  refresh();
  window.setInterval(refresh, 10000);
})();
