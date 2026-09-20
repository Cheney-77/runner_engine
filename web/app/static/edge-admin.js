"use strict";

(() => {
  const byId = (id) => document.getElementById(id);

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function targetKey(target) {
    return [
      target?.os || "unknown",
      target?.arch || "unknown",
      target?.pythonVersion || "unknown",
    ].join("|");
  }

  function targetLabel(target) {
    return `${target?.os || "—"} · ${target?.arch || "—"} · Python ${target?.pythonVersion || "—"}`;
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

  function renderMetrics(deployments, bundles) {
    const environments = new Set(
      deployments.map((item) => targetKey(item.targetPlatform))
    );

    const latestRevisions = new Map();
    for (const bundle of bundles) {
      const key = targetKey(bundle.targetPlatform);
      if (!latestRevisions.has(key)) {
        latestRevisions.set(key, bundle.revision);
      }
    }

    const metrics = byId("metrics");
    metrics.replaceChildren(
      metric("共享环境", environments.size),
      metric("当前算子", deployments.length),
      metric("历史 Bundle", bundles.length),
      metric("最新环境 Revision", latestRevisions.size
        ? Math.max(...latestRevisions.values())
        : 0),
    );
  }

  function requirementsNode(requirements) {
    const code = document.createElement("code");
    code.textContent = Array.isArray(requirements) && requirements.length
      ? requirements.join(", ")
      : "无显式依赖";
    return code;
  }

  function renderEnvironments(deployments, bundles) {
    const groups = new Map();

    for (const deployment of deployments) {
      const key = targetKey(deployment.targetPlatform);
      if (!groups.has(key)) {
        groups.set(key, {
          target: deployment.targetPlatform,
          deployments: [],
        });
      }
      groups.get(key).deployments.push(deployment);
    }

    const latestByTarget = new Map();
    for (const bundle of bundles) {
      const key = targetKey(bundle.targetPlatform);
      if (!latestByTarget.has(key)) latestByTarget.set(key, bundle);
    }

    const container = byId("environmentList");
    container.replaceChildren();

    if (!groups.size) {
      const empty = document.createElement("div");
      empty.className = "edge-empty";
      empty.textContent = "还没有 NiFi Native 算子加入边端共享环境。";
      container.append(empty);
      return;
    }

    for (const [key, group] of groups) {
      const card = document.createElement("article");
      card.className = "edge-env-card";

      const header = document.createElement("header");
      const title = document.createElement("h2");
      title.textContent = targetLabel(group.target);

      const bundle = latestByTarget.get(key);
      const badge = document.createElement("span");
      badge.className = "edge-status";
      badge.textContent = bundle
        ? `Revision ${bundle.revision}`
        : "尚无 Bundle";

      header.append(title, badge);

      const wrap = document.createElement("div");
      wrap.className = "edge-table-wrap";

      const table = document.createElement("table");
      table.className = "edge-table";
      table.innerHTML = `
        <thead>
          <tr>
            <th>算子</th>
            <th>Workspace</th>
            <th>Package</th>
            <th>直接依赖</th>
            <th>更新时间</th>
          </tr>
        </thead>
      `;

      const tbody = document.createElement("tbody");
      for (const item of group.deployments) {
        const row = document.createElement("tr");

        const operator = document.createElement("td");
        operator.textContent = item.displayName || item.name || item.operatorId;

        const workspace = document.createElement("td");
        workspace.textContent = item.workspace || "—";

        const packageName = document.createElement("td");
        const packageCode = document.createElement("code");
        packageCode.textContent = item.packageName || "—";
        packageName.append(packageCode);

        const requirements = document.createElement("td");
        requirements.append(requirementsNode(item.requirements));

        const updated = document.createElement("td");
        updated.textContent = formatTime(item.updatedAt);

        row.append(operator, workspace, packageName, requirements, updated);
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
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 7;
      cell.className = "edge-empty";
      cell.textContent = "暂无 Edge Bundle。";
      row.append(cell);
      tbody.append(row);
      return;
    }

    for (const bundle of bundles) {
      const row = document.createElement("tr");

      const revision = document.createElement("td");
      revision.textContent = String(bundle.revision ?? "—");

      const target = document.createElement("td");
      target.textContent = targetLabel(bundle.targetPlatform);

      const processors = document.createElement("td");
      processors.textContent = String(bundle.manifest?.processors?.length ?? "—");

      const packages = document.createElement("td");
      packages.textContent = String(bundle.manifest?.dependencyPackageCount ?? "—");

      const sha = document.createElement("td");
      const code = document.createElement("code");
      code.textContent = bundle.artifactSha256
        ? `${bundle.artifactSha256.slice(0, 14)}…`
        : "—";
      sha.append(code);

      const created = document.createElement("td");
      created.textContent = formatTime(bundle.createdAt);

      const actions = document.createElement("td");
      const link = document.createElement("a");
      link.className = "text-button";
      link.href = `/api/edge/bundles/${encodeURIComponent(bundle.bundleId)}/download`;
      link.textContent = "下载";
      actions.append(link);

      row.append(revision, target, processors, packages, sha, created, actions);
      tbody.append(row);
    }
  }

  async function refresh() {
    byId("refreshBtn").disabled = true;

    try {
      const [deploymentsResponse, bundlesResponse] = await Promise.all([
        getJson("/api/edge/deployments"),
        getJson("/api/edge/bundles?limit=100"),
      ]);

      const deployments = deploymentsResponse?.deployments || [];
      const bundles = bundlesResponse?.bundles || [];

      renderMetrics(deployments, bundles);
      renderEnvironments(deployments, bundles);
      renderBundles(bundles);
      showNotice(`已加载 ${deployments.length} 个当前算子和 ${bundles.length} 个 Bundle。`, "success");
    } catch (error) {
      showNotice(error.message || String(error), "error");
    } finally {
      byId("refreshBtn").disabled = false;
    }
  }

  byId("refreshBtn").addEventListener("click", refresh);
  refresh();
})();
