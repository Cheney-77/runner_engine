"use strict";

(() => {
  const requestRows = document.getElementById("requestRows");
  const feedback = document.getElementById("feedback");
  let enabled = false;
  let busy = false;
  let latestExecutionByRequest = new Map();

  function show(message, kind = "") {
    if (!feedback) return;
    feedback.textContent = message;
    feedback.className = `feedback ${kind}`.trim();
  }

  async function api(method, path, body) {
    const options = {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers: {Accept: "application/json"},
    };

    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Admin-Request"] = "1";
      options.body = JSON.stringify(body);
    }

    const response = await fetch(path, options);
    const text = await response.text();

    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      payload = {detail: "响应不是 JSON"};
    }

    if (!response.ok) {
      throw new Error(
        typeof payload.detail === "string"
          ? payload.detail
          : `请求失败 HTTP ${response.status}`,
      );
    }

    return payload;
  }

  function actionCell(row) {
    return row.cells.length >= 5 ? row.cells[4] : null;
  }

  function requestId(row) {
    return row.cells.length ? row.cells[0].textContent.trim() : "";
  }

  function status(row) {
    return row.cells.length >= 3 ? row.cells[2].textContent.trim() : "";
  }

  function decorateRows() {
    if (!enabled || !requestRows) return;

    for (const row of requestRows.querySelectorAll("tr")) {
      if (status(row) !== "DRAFT") continue;

      const cell = actionCell(row);
      const id = requestId(row);
      if (!cell || !id || cell.querySelector(".lifecycle-execute")) continue;

      const latest = latestExecutionByRequest.get(id);
      if (latest?.status === "SUCCEEDED" || latest?.status === "PARTIAL") {
        continue;
      }

      const button = document.createElement("button");
      button.type = "button";
      button.className = "button danger small lifecycle-execute";
      button.textContent = latest?.status === "WAITING" ? "继续清理" : "执行清理";
      button.addEventListener("click", () => execute(id));

      cell.prepend(button);
    }
  }

  async function execute(id) {
    if (busy) return;

    const confirmed = window.confirm(
      "系统会重新检查引用，依次关闭 Build / Publish / Runner 新入口，"
      + "等待活动 invocation 和 sandbox 排空，再由 Build Service 删除 Harbor artifact。"
      + "\n\n历史 Build/Publish/Runner 记录不会删除。"
      + "\nHarbor blob 的物理空间需要后续 GC 才会真正释放。"
      + "\n\n确认继续？"
    );
    if (!confirmed) return;

    busy = true;
    show("正在通过资源 Owner Service 执行安全清理…");

    try {
      const result = await api(
        "POST",
        `/admin/api/cleanup-requests/${encodeURIComponent(id)}/execute`,
        {},
      );

      if (result.status === "WAITING") {
        show(
          "Retirement gate 已生效，正在等待活动 invocation / sandbox 排空。"
          + "稍后再次点击“继续清理”即可。",
          "warning",
        );
      } else if (result.status === "BLOCKED") {
        show("清理被当前引用阻止，请先处理 blocking references。", "warning");
      } else if (result.status === "SUCCEEDED") {
        show(
          "Runtime Environment 已安全退役，Harbor artifact 已删除。"
          + "物理 blob 空间将在 Harbor GC 后释放。",
          "success",
        );
      }

      window.setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      show(error.message || String(error), "error");
    } finally {
      busy = false;
    }
  }

  async function init() {
    try {
      const [capabilities, executions] = await Promise.all([
        api("GET", "/admin/api/lifecycle-capabilities"),
        api("GET", "/admin/api/cleanup-executions?limit=200"),
      ]);

      enabled = Boolean(capabilities.enabled);
      latestExecutionByRequest = new Map();

      for (const execution of executions.executions || []) {
        if (!latestExecutionByRequest.has(execution.requestId)) {
          latestExecutionByRequest.set(execution.requestId, execution);
        }
      }
    } catch {
      enabled = false;
      latestExecutionByRequest = new Map();
    }

    decorateRows();

    if (requestRows) {
      new MutationObserver(decorateRows).observe(requestRows, {
        childList: true,
        subtree: true,
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, {once: true});
  } else {
    init();
  }
})();
