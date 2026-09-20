"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const pretty = (value) => JSON.stringify(value, null, 2);
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

  function ensureStyles() {
    if (document.querySelector('link[href="/static/edge.css"]')) return;

    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/static/edge.css";
    document.head.append(link);
  }

  function showInlineError(message) {
    const panel = byId("edgeTargetError");
    if (!panel) return;

    panel.textContent = message || "";
    panel.classList.toggle("hidden", !message);
  }

  function setOptionsTarget(platform) {
    const textarea = byId("options");
    if (!textarea) return;

    let options = {};
    try {
      const parsed = JSON.parse(textarea.value || "{}");
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        options = parsed;
      }
    } catch {
      showInlineError("高级 options JSON 无效，请先修复后再选择边端平台。");
      return;
    }

    options.targetPlatform = {
      os: platform.os,
      arch: platform.arch,
    };
    textarea.value = pretty(options);
    textarea.dispatchEvent(new Event("input", {bubbles: true}));
    showInlineError("");
  }

  function currentTargetFromOptions() {
    const textarea = byId("options");
    if (!textarea) return null;

    try {
      const options = JSON.parse(textarea.value || "{}");
      const target = options?.targetPlatform;
      if (!target || typeof target !== "object") return null;

      return {
        os: String(target.os || ""),
        arch: String(target.arch || ""),
      };
    } catch {
      return null;
    }
  }

  function injectNavigation() {
    const topbar = document.querySelector(".topbar-right");
    if (!topbar || document.querySelector(".edge-console-links")) return;

    const nav = document.createElement("nav");
    nav.className = "edge-console-links";
    nav.setAttribute("aria-label", "Edge Native 管理入口");
    nav.innerHTML = [
      '<a href="/admin/edge">边端管理</a>',
      '<a href="/ops/edge">运维观测</a>',
    ].join("");

    topbar.prepend(nav);
  }

  function injectTargetSelector() {
    const backendDescription = byId("backendDescription");
    if (!backendDescription || byId("edgeTargetSection")) return;

    const section = document.createElement("section");
    section.id = "edgeTargetSection";
    section.className = "edge-target-section hidden";
    section.innerHTML = `
      <div class="edge-target-heading">
        <div>
          <strong>边端目标平台</strong>
          <span>NiFi Native 会加入该平台对应的共享 Python 依赖环境。</span>
        </div>
        <span id="edgeTargetSelected" class="edge-target-selected">尚未选择</span>
      </div>
      <div id="edgeTargetGrid" class="edge-target-grid" aria-label="边端目标平台"></div>
      <div id="edgeTargetError" class="edge-target-error hidden" role="alert"></div>
      <p class="edge-target-note">
        Python 版本由平台统一配置，不由浏览器指定。发布时会将当前用户在同一目标平台下的
        NiFi Native 依赖做全量 uv 解析；存在不可调和冲突时发布会被拒绝。
      </p>
    `;

    backendDescription.insertAdjacentElement("afterend", section);
  }

  async function loadPlatforms() {
    const response = await fetch("/api/edge/platforms", {
      headers: {Accept: "application/json"},
    });

    if (!response.ok) {
      throw new Error(`读取边端平台失败：HTTP ${response.status}`);
    }

    const data = await response.json();
    return Array.isArray(data?.platforms) ? data.platforms : [];
  }

  function renderPlatforms(platforms) {
    const grid = byId("edgeTargetGrid");
    if (!grid) return;

    grid.replaceChildren();

    for (const platform of platforms) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "edge-target-card";
      button.dataset.os = platform.os;
      button.dataset.arch = platform.arch;
      button.innerHTML = `
        <strong>${platform.label || `${platform.os} · ${platform.arch}`}</strong>
        <span>Python ${platform.pythonVersion || "平台默认"}</span>
      `;

      button.addEventListener("click", () => {
        setOptionsTarget(platform);
        syncSelection(platforms);
      });

      grid.append(button);
    }

    syncSelection(platforms);
  }

  function syncSelection(platforms) {
    const selected = currentTargetFromOptions();
    const label = byId("edgeTargetSelected");

    document.querySelectorAll(".edge-target-card").forEach((button) => {
      const active = selected
        && button.dataset.os === selected.os
        && button.dataset.arch === selected.arch;

      button.classList.toggle("selected", !!active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });

    const platform = platforms.find((item) =>
      selected && item.os === selected.os && item.arch === selected.arch
    );

    if (label) {
      label.textContent = platform
        ? `${platform.label} · Python ${platform.pythonVersion}`
        : "尚未选择";
    }
  }

  function syncVisibility(platforms) {
    const native = byId("backend")?.value === "nifi_native";
    byId("edgeTargetSection")?.classList.toggle("hidden", !native);

    if (native) {
      syncSelection(platforms);
    }
  }

  function guardNativeAction(event) {
    if (byId("backend")?.value !== "nifi_native") return;
    if (currentTargetFromOptions()) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    showInlineError("发布 NiFi Native 前必须选择边端目标平台。");
    byId("edgeTargetSection")?.scrollIntoView({behavior: "smooth", block: "center"});
  }

  function ensureOutcomePanel() {
    let panel = byId("edgePublishOutcome");
    if (panel) return panel;

    const publishPanel = byId("publishPanel");
    if (!publishPanel) return null;

    panel = document.createElement("section");
    panel.id = "edgePublishOutcome";
    panel.className = "panel edge-publish-outcome hidden";
    publishPanel.insertAdjacentElement("afterend", panel);
    return panel;
  }

  function renderConflict(detail) {
    const panel = ensureOutcomePanel();
    if (!panel) return;

    const members = Array.isArray(detail?.environmentMembers)
      ? detail.environmentMembers
      : [];

    panel.classList.remove("hidden");
    panel.innerHTML = `
      <div class="edge-outcome-title error">
        <div>
          <span class="edge-outcome-kicker">EDGE DEPENDENCY CONFLICT</span>
          <h2>共享依赖环境无法完成解析</h2>
        </div>
        <span class="edge-status error">发布被拒绝</span>
      </div>
      <p>${escapeHtml(detail?.message || "当前算子无法加入所选边端共享环境。")}</p>
      <div class="edge-conflict-grid">
        <div>
          <span class="edge-meta">当前算子依赖</span>
          <code>${escapeHtml((detail?.candidate?.requirements || []).join(", ") || "无显式依赖")}</code>
        </div>
        <div>
          <span class="edge-meta">已有算子数量</span>
          <strong>${members.length}</strong>
        </div>
      </div>
      <div class="edge-member-list">
        ${members.map((member) => `
          <div class="edge-member-row">
            <strong>${escapeHtml(member.packageName || member.operatorId)}</strong>
            <code>${escapeHtml((member.requirements || []).join(", ") || "无显式依赖")}</code>
          </div>
        `).join("") || '<p class="muted">当前环境没有其他已发布算子。</p>'}
      </div>
      <details class="developer-details">
        <summary>查看 uv resolver 详情</summary>
        <pre class="json-view">${escapeHtml(detail?.resolverError || "")}</pre>
      </details>
    `;
  }

  function renderBundle(result) {
    const bundle = result?.edgeBundle;
    if (!bundle) return;

    const panel = ensureOutcomePanel();
    if (!panel) return;

    const target = result.targetPlatform || {};
    panel.classList.remove("hidden");
    panel.innerHTML = `
      <div class="edge-outcome-title">
        <div>
          <span class="edge-outcome-kicker">EDGE BUNDLE READY</span>
          <h2>边端全量包已生成</h2>
        </div>
        <span class="edge-status">Revision ${bundle.revision ?? "—"}</span>
      </div>
      <div class="edge-bundle-facts">
        <div><span class="edge-meta">目标平台</span><strong>${escapeHtml(target.os || "—")} · ${escapeHtml(target.arch || "—")}</strong></div>
        <div><span class="edge-meta">Python</span><strong>${escapeHtml(target.pythonVersion || "—")}</strong></div>
        <div><span class="edge-meta">Processor 数量</span><strong>${bundle.processorCount ?? "—"}</strong></div>
        <div><span class="edge-meta">依赖包数量</span><strong>${bundle.dependencyPackageCount ?? "—"}</strong></div>
      </div>
      <div class="edge-bundle-actions">
        <a class="button primary" href="/api/edge/bundles/${encodeURIComponent(bundle.bundleId)}/download">
          下载 Edge Bundle
        </a>
        <a class="button secondary" href="/admin/edge">查看边端环境</a>
        <a class="button secondary" href="/ops/edge">查看发布记录</a>
      </div>
      <details class="developer-details">
        <summary>Bundle 校验信息</summary>
        <pre class="json-view">${pretty({
          bundleId: bundle.bundleId,
          artifactSha256: bundle.artifactSha256,
          dependencyLockSha256: bundle.dependencyLockSha256,
          artifactRef: bundle.artifactRef,
        })}</pre>
      </details>
    `;
  }

  function observePublishResponse() {
    const responseNode = byId("publishResponse");
    if (!responseNode) return;

    const render = () => {
      const raw = responseNode.textContent?.trim();
      if (!raw) return;

      let payload;
      try {
        payload = JSON.parse(raw);
      } catch {
        return;
      }

      if (payload?.detail?.code === "EDGE_DEPENDENCY_CONFLICT") {
        renderConflict(payload.detail);
        return;
      }

      if (payload?.result?.edgeBundle) {
        renderBundle(payload.result);
      }
    };

    new MutationObserver(render).observe(responseNode, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  async function init() {
    ensureStyles();
    injectNavigation();
    injectTargetSelector();
    observePublishResponse();

    const backend = byId("backend");
    const options = byId("options");
    const compile = byId("compileBtn");
    const publish = byId("publishBtn");

    let platforms = [];

    try {
      platforms = await loadPlatforms();
      renderPlatforms(platforms);
    } catch (error) {
      showInlineError(error.message || String(error));
    }

    backend?.addEventListener("change", () => {
      queueMicrotask(() => syncVisibility(platforms));
    });

    options?.addEventListener("input", () => {
      if (backend?.value === "nifi_native") syncSelection(platforms);
    });

    compile?.addEventListener("click", guardNativeAction, true);
    publish?.addEventListener("click", guardNativeAction, true);

    syncVisibility(platforms);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, {once: true});
  } else {
    init();
  }
})();
