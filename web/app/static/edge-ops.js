"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const Edge = globalThis.EdgeIdentity;
  let state = {userId: null, operations: [], bundles: []};

  function showNotice(message, kind = "") {
    byId("noticeText").textContent = message;
    byId("notice").className = kind ? `notice ${kind}` : "notice";
  }

  async function getJson(path) {
    const response = await fetch(path, {headers: {Accept: "application/json"}});
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data?.detail;
      throw new Error(typeof detail === "string" ? detail : `HTTP ${response.status}: ${JSON.stringify(detail ?? data)}`);
    }
    return data;
  }

  function element(tag, text, className = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function metric(label, value) {
    const card = element("div", undefined, "edge-metric");
    card.append(element("span", label), element("strong", value));
    return card;
  }

  function renderMetrics(operations, bundles) {
    const failed = operations.filter((item) => item.jobStatus === "FAILED").length;
    const ready = operations.filter((item) => item.jobStatus === "READY").length;
    byId("metrics").replaceChildren(
      metric("最近任务（已加载）", operations.length),
      metric("成功", ready),
      metric("失败", failed),
      metric("涉及已绑定机器", Edge.distinctMachines([...operations, ...bundles])),
    );
  }

  function renderOperations(operations) {
    const container = byId("operationList");
    container.replaceChildren();
    if (!operations.length) {
      container.append(element("div", "当前筛选条件下暂无 NiFi Native 发布任务。", "edge-empty"));
      return;
    }

    for (const item of operations) {
      const identity = Edge.identityOf(item);
      const card = element("article", undefined, "edge-op-card");
      const header = element("header");
      const title = element("h2", item.displayName || item.name || item.operatorId);
      const failed = item.jobStatus === "FAILED";
      const status = element("span", item.jobStatus || "UNKNOWN", failed ? "edge-status error" : "edge-status");
      header.append(title, status);

      const tableWrap = element("div", undefined, "edge-table-wrap");
      const table = element("table", undefined, "edge-table");
      const body = element("tbody");
      const rows = [
        ["目标平台", Edge.targetLabel(Edge.targetOf(item))],
        ["edge_name", Edge.fieldLabel(identity.edgeName, identity)],
        ["token_pair", Edge.fieldLabel(identity.tokenPair, identity)],
        ["Workspace", item.workspace || "—"],
        ["Job ID", item.jobId || "—"],
        ["Variant ID", item.variantId || "—"],
        ["Artifact", item.artifactRef || "—"],
        ["Started", Edge.formatTime(item.startedAt || item.createdAt)],
        ["Finished", Edge.formatTime(item.finishedAt)],
      ];
      for (const [name, value] of rows) {
        const row = element("tr");
        row.append(element("th", name), element("td", value));
        body.append(row);
      }
      table.append(body);
      tableWrap.append(table);
      card.append(header, tableWrap);
      if (item.errorMessage) card.append(element("div", item.errorMessage, "edge-error-box"));
      container.append(card);
    }
  }

  function renderBundles(bundles) {
    const tbody = byId("bundleRows");
    tbody.replaceChildren();
    if (!bundles.length) {
      const row = element("tr");
      const cell = element("td", "当前筛选条件下暂无成功 Bundle。", "edge-empty");
      cell.colSpan = 10;
      row.append(cell);
      tbody.append(row);
      return;
    }

    for (const bundle of bundles) {
      const identity = Edge.identityOf(bundle);
      const row = element("tr");
      const token = element("td");
      token.append(element("code", Edge.fieldLabel(identity.tokenPair, identity)));
      const lock = element("td");
      lock.append(element("code", bundle.lockSha256 ? `${bundle.lockSha256.slice(0, 12)}…` : "—"));
      const artifact = element("td");
      artifact.append(element("code", bundle.artifactSha256 ? `${bundle.artifactSha256.slice(0, 12)}…` : "—"));
      const actions = element("td");
      if (bundle.bundleId) {
        const link = element("a", "下载", "text-button");
        link.href = `/api/edge/bundles/${encodeURIComponent(bundle.bundleId)}/download`;
        actions.append(link);
      }
      row.append(
        element("td", bundle.revision ?? "—"), element("td", Edge.targetLabel(Edge.targetOf(bundle))),
        element("td", Edge.fieldLabel(identity.edgeName, identity)), token,
        element("td", bundle.manifest?.processors?.length ?? "—"),
        element("td", bundle.manifest?.dependencyPackageCount ?? "—"), lock, artifact,
        element("td", Edge.formatTime(bundle.createdAt)), actions,
      );
      tbody.append(row);
    }
  }

  function render() {
    const tokenQuery = byId("tokenPairFilter").value;
    const nameQuery = byId("edgeNameFilter").value;
    const operations = state.operations.filter((item) => Edge.matches(item, tokenQuery, nameQuery));
    const bundles = state.bundles.filter((item) => Edge.matches(item, tokenQuery, nameQuery));
    renderMetrics(operations, bundles);
    renderOperations(operations);
    renderBundles(bundles);
  }

  async function refresh() {
    byId("refreshBtn").disabled = true;
    try {
      const [operationsResponse, bundlesResponse] = await Promise.all([
        getJson("/api/edge/operations?limit=120"), getJson("/api/edge/bundles?limit=120"),
      ]);
      state = {
        userId: operationsResponse?.userId ?? bundlesResponse?.userId ?? null,
        operations: Array.isArray(operationsResponse?.operations) ? operationsResponse.operations : [],
        bundles: Array.isArray(bundlesResponse?.bundles) ? bundlesResponse.bundles : [],
      };
      render();
      showNotice(`已加载最近 ${state.operations.length} 条任务记录和 ${state.bundles.length} 个 Bundle。统计基于已加载数据。`, "success");
    } catch (error) {
      showNotice(error.message || String(error), "error");
    } finally {
      byId("refreshBtn").disabled = false;
    }
  }

  byId("refreshBtn").addEventListener("click", refresh);
  byId("tokenPairFilter").addEventListener("input", render);
  byId("edgeNameFilter").addEventListener("input", render);
  byId("clearFiltersBtn").addEventListener("click", () => {
    byId("tokenPairFilter").value = "";
    byId("edgeNameFilter").value = "";
    render();
  });
  refresh();
})();
