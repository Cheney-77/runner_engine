"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const Edge = globalThis.EdgeIdentity;
  let state = {userId: null, deployments: [], bundles: []};

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

  function filters() {
    return [byId("tokenPairFilter").value, byId("edgeNameFilter").value];
  }

  function renderMetrics(deployments, bundles) {
    const environments = new Set(deployments.map((item) => Edge.scopeKey(item, state.userId)));
    byId("metrics").replaceChildren(
      metric("当前独立环境", environments.size),
      metric("当前已绑定机器", Edge.distinctMachines(deployments)),
      metric("当前算子", deployments.length),
      metric("最近 Bundle（已加载）", bundles.length),
    );
  }

  function requirementsNode(requirements) {
    return element("code", Array.isArray(requirements) && requirements.length ? requirements.join(", ") : "无显式依赖");
  }

  function identityDetails(item) {
    const identity = Edge.identityOf(item);
    const meta = element("div", undefined, "edge-identity-meta");
    meta.append(
      element("span", `User ID：${item.userId ?? state.userId ?? "—"}`),
      element("span", `edge_name：${Edge.fieldLabel(identity.edgeName, identity)}`),
      element("span", `token_pair：${Edge.fieldLabel(identity.tokenPair, identity)}`),
    );
    return meta;
  }

  function renderEnvironments(deployments, bundles) {
    const groups = new Map();
    for (const deployment of deployments) {
      const key = Edge.scopeKey(deployment, state.userId);
      if (!groups.has(key)) groups.set(key, {sample: deployment, deployments: []});
      groups.get(key).deployments.push(deployment);
    }

    const latestByScope = new Map();
    for (const bundle of [...bundles].sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")))) {
      const key = Edge.scopeKey(bundle, state.userId);
      if (!latestByScope.has(key)) latestByScope.set(key, bundle);
    }

    const container = byId("environmentList");
    container.replaceChildren();
    if (!groups.size) {
      container.append(element("div", "当前筛选条件下没有已部署的 NiFi Native 算子。", "edge-empty"));
      return;
    }

    for (const [key, group] of groups) {
      const card = element("article", undefined, "edge-env-card");
      const header = element("header");
      const heading = element("div", undefined, "edge-env-heading");
      heading.append(element("h2", Edge.targetLabel(Edge.targetOf(group.sample))), identityDetails(group.sample));
      const bundle = latestByScope.get(key);
      const badge = element("span", bundle ? `最近 Revision ${bundle.revision}` : "最近列表中无 Bundle", "edge-status");
      header.append(heading, badge);

      const wrap = element("div", undefined, "edge-table-wrap");
      const table = element("table", undefined, "edge-table");
      const thead = element("thead");
      const headings = element("tr");
      ["算子", "Workspace", "Package", "直接依赖", "更新时间"].forEach((label) => headings.append(element("th", label)));
      thead.append(headings);
      table.append(thead);
      const tbody = element("tbody");
      for (const item of group.deployments) {
        const row = element("tr");
        const packageName = element("td");
        packageName.append(element("code", item.packageName || "—"));
        const requirements = element("td");
        requirements.append(requirementsNode(item.requirements));
        row.append(
          element("td", item.displayName || item.name || item.operatorId),
          element("td", item.workspace || "—"), packageName,
          requirements, element("td", Edge.formatTime(item.updatedAt)),
        );
        tbody.append(row);
      }
      table.append(tbody);
      wrap.append(table);
      card.append(header, wrap);
      container.append(card);
    }
  }

  function renderBundles(bundles) {
    const tbody = byId("bundleRows");
    tbody.replaceChildren();
    if (!bundles.length) {
      const cell = element("td", "当前筛选条件下暂无 Edge Bundle。", "edge-empty");
      cell.colSpan = 9;
      const row = element("tr");
      row.append(cell);
      tbody.append(row);
      return;
    }

    for (const bundle of bundles) {
      const identity = Edge.identityOf(bundle);
      const row = element("tr");
      const name = element("td", Edge.fieldLabel(identity.edgeName, identity));
      const token = element("td");
      token.append(element("code", Edge.fieldLabel(identity.tokenPair, identity)));
      const sha = element("td");
      sha.append(element("code", bundle.artifactSha256 ? `${bundle.artifactSha256.slice(0, 14)}…` : "—"));
      const actions = element("td");
      if (bundle.bundleId) {
        const link = element("a", "下载", "text-button");
        link.href = `/api/edge/bundles/${encodeURIComponent(bundle.bundleId)}/download`;
        actions.append(link);
      }
      row.append(
        element("td", bundle.revision ?? "—"), element("td", Edge.targetLabel(Edge.targetOf(bundle))),
        name, token, element("td", bundle.manifest?.processors?.length ?? "—"),
        element("td", bundle.manifest?.dependencyPackageCount ?? "—"), sha,
        element("td", Edge.formatTime(bundle.createdAt)), actions,
      );
      tbody.append(row);
    }
  }

  function render() {
    const [tokenQuery, nameQuery] = filters();
    const deployments = state.deployments.filter((item) => Edge.matches(item, tokenQuery, nameQuery));
    const bundles = state.bundles.filter((item) => Edge.matches(item, tokenQuery, nameQuery));
    renderMetrics(deployments, bundles);
    renderEnvironments(deployments, bundles);
    renderBundles(bundles);
  }

  async function refresh() {
    byId("refreshBtn").disabled = true;
    try {
      const [deploymentsResponse, bundlesResponse] = await Promise.all([
        getJson("/api/edge/deployments"), getJson("/api/edge/bundles?limit=100"),
      ]);
      state = {
        userId: deploymentsResponse?.userId ?? bundlesResponse?.userId ?? null,
        deployments: Array.isArray(deploymentsResponse?.deployments) ? deploymentsResponse.deployments : [],
        bundles: Array.isArray(bundlesResponse?.bundles) ? bundlesResponse.bundles : [],
      };
      render();
      showNotice(`已加载 ${state.deployments.length} 个当前算子和最近 ${state.bundles.length} 个 Bundle。筛选和统计基于已加载数据。`, "success");
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
