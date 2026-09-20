"use strict";

(() => {
  const byId = (id) => document.getElementById(id);

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function targetFromOptions(options) {
    return options?.targetPlatform || options?.target_platform || null;
  }

  function targetLabel(target) {
    if (!target) return "—";
    return `${target.os || "—"} · ${target.arch || "—"}${target.pythonVersion ? ` · Python ${target.pythonVersion}` : ""}`;
  }

  function showNotice(message, kind = "") {
    byId("noticeText").textContent = message;
    byId("notice").className = kind ? `notice ${kind}` : "notice";
  }

  async function getJson(path) {
    const response = await fetch(path, {headers: {Accept: "application/json"}});
    const data = await response.json().catch(() => null);

    if (!response.ok) {
      const detail = data?.detail;
      throw new Error(
        typeof detail === "string"
          ? detail
          : `HTTP ${response.status}: ${JSON.stringify(detail ?? data)}`
      );
    }

    return data;
  }

  function metric(label, value) {
    const card = document.createElement("div");
    card.className = "edge-metric";

    const caption = document.createElement("span");
    caption.textContent = label;

    const strong = document.createElement("strong");
    strong.textContent = String(value);

    card.append(caption, strong);
    return card;
  }

  function renderMetrics(operations, bundles) {
    const failed = operations.filter((item) => item.jobStatus === "FAILED").length;
    const ready = operations.filter((item) => item.jobStatus === "READY").length;
    const latestRevision = bundles.reduce(
      (value, item) => Math.max(value, Number(item.revision || 0)),
      0,
    );

    byId("metrics").replaceChildren(
      metric("最近任务", operations.length),
      metric("成功", ready),
      metric("失败", failed),
      metric("最高 Revision", latestRevision),
    );
  }

  function renderOperations(operations) {
    const container = byId("operationList");
    container.replaceChildren();

    if (!operations.length) {
      const empty = document.createElement("div");
      empty.className = "edge-empty";
      empty.textContent = "暂无 NiFi Native 发布任务。";
      container.append(empty);
      return;
    }

    for (const item of operations) {
      const card = document.createElement("article");
      card.className = "edge-op-card";

      const header = document.createElement("header");
      const title = document.createElement("h2");
      title.textContent = item.displayName || item.name || item.operatorId;

      const status = document.createElement("span");
      const failed = item.jobStatus === "FAILED";
      status.className = failed ? "edge-status error" : "edge-status";
      status.textContent = item.jobStatus || "UNKNOWN";

      header.append(title, status);

      const tableWrap = document.createElement("div");
      tableWrap.className = "edge-table-wrap";

      const table = document.createElement("table");
      table.className = "edge-table";

      const body = document.createElement("tbody");
      const rows = [
        ["Target", targetLabel(targetFromOptions(item.options))],
        ["Workspace", item.workspace || "—"],
        ["Job ID", item.jobId || "—"],
        ["Variant ID", item.variantId || "—"],
        ["Artifact", item.artifactRef || "—"],
        ["Started", formatTime(item.startedAt || item.createdAt)],
        ["Finished", formatTime(item.finishedAt)],
      ];

      for (const [name, value] of rows) {
        const row = document.createElement("tr");
        const key = document.createElement("th");
        key.textContent = name;

        const cell = document.createElement("td");
        cell.textContent = String(value);

        row.append(key, cell);
        body.append(row);
      }

      table.append(body);
      tableWrap.append(table);
      card.append(header, tableWrap);

      if (item.errorMessage) {
        const error = document.createElement("div");
        error.className = "edge-error-box";
        error.textContent = item.errorMessage;
        card.append(error);
      }

      container.append(card);
    }
  }

  function renderBundles(bundles) {
    const tbody = byId("bundleRows");
    tbody.replaceChildren();

    if (!bundles.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 7;
      cell.className = "edge-empty";
      cell.textContent = "暂无成功 Bundle。";
      row.append(cell);
      tbody.append(row);
      return;
    }

    for (const bundle of bundles) {
      const row = document.createElement("tr");

      const values = [
        bundle.revision ?? "—",
        targetLabel(bundle.targetPlatform),
        bundle.manifest?.processors?.length ?? "—",
        bundle.manifest?.dependencyPackageCount ?? "—",
        bundle.lockSha256 ? `${bundle.lockSha256.slice(0, 12)}…` : "—",
        bundle.artifactSha256 ? `${bundle.artifactSha256.slice(0, 12)}…` : "—",
        formatTime(bundle.createdAt),
      ];

      values.forEach((value, index) => {
        const cell = document.createElement("td");
        if (index === 4 || index === 5) {
          const code = document.createElement("code");
          code.textContent = String(value);
          cell.append(code);
        } else {
          cell.textContent = String(value);
        }
        row.append(cell);
      });

      tbody.append(row);
    }
  }

  async function refresh() {
    byId("refreshBtn").disabled = true;

    try {
      const [operationsResponse, bundlesResponse] = await Promise.all([
        getJson("/api/edge/operations?limit=120"),
        getJson("/api/edge/bundles?limit=120"),
      ]);

      const operations = operationsResponse?.operations || [];
      const bundles = bundlesResponse?.bundles || [];

      renderMetrics(operations, bundles);
      renderOperations(operations);
      renderBundles(bundles);
      showNotice(`已加载 ${operations.length} 条任务记录和 ${bundles.length} 个 Bundle。`, "success");
    } catch (error) {
      showNotice(error.message || String(error), "error");
    } finally {
      byId("refreshBtn").disabled = false;
    }
  }

  byId("refreshBtn").addEventListener("click", refresh);
  refresh();
})();
