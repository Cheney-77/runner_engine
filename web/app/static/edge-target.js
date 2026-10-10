"use strict";

/* Demo only: enter device codes here. Production can populate the same fields from its device selector. */
(function initializeEdgeTargetForm() {
  const backend = document.getElementById("backend");
  const advanced = document.getElementById("advancedOptions");
  const optionsEditor = document.getElementById("options");
  if (!backend || !advanced || !optionsEditor || typeof window.currentOptions !== "function") return;

  const panel = document.createElement("section");
  panel.id = "edgeTargetFields";
  panel.className = "edge-target-section hidden";
  panel.innerHTML = `
    <div class="edge-target-heading"><div>
      <strong>目标边端机器</strong>
      <span>先在上方选择目标平台，再输入机器编码。相同平台的不同机器拥有独立 Python 依赖环境。</span>
    </div></div>
    <div class="edge-target-grid">
      <div class="field">
        <label for="edgeTokenPair">token_pair</label>
        <input id="edgeTokenPair" type="text" autocomplete="off" maxlength="128" spellcheck="false" placeholder="例如：pair-001">
      </div>
      <div class="field">
        <label for="edgeName">edge_name</label>
        <input id="edgeName" type="text" autocomplete="off" maxlength="128" spellcheck="false" placeholder="例如：factory-edge-01">
      </div>
    </div>
    <p id="edgeIdentityPreview" class="edge-target-note" aria-live="polite"></p>
    <p class="edge-target-note">Demo 手动输入；正式平台通过设备下拉框提供这两个字段，Publish Service 不存储设备主表。</p>`;
  advanced.before(panel);

  function correctPlatformCopy() {
    const section = document.getElementById("edgeTargetSection");
    if (!section || typeof section.querySelector !== "function") return false;
    const description = section.querySelector(".edge-target-heading span");
    const note = section.querySelector(".edge-target-note");
    if (description) description.textContent = "NiFi Native 会加入目标平台与指定机器共同决定的 Python 共享环境。";
    if (note) note.textContent = "Python 版本由服务端配置。依赖仅在同一用户、平台、Python 版本及 token_pair + edge_name 下合并解析。";
    return true;
  }
  if (!correctPlatformCopy() && document.body) {
    const copyObserver = new MutationObserver(() => {
      if (correctPlatformCopy()) copyObserver.disconnect();
    });
    copyObserver.observe(document.body, {childList: true, subtree: true});
  }

  const tokenInput = document.getElementById("edgeTokenPair");
  const nameInput = document.getElementById("edgeName");
  const preview = document.getElementById("edgeIdentityPreview");
  let synchronizing = false;

  function readOptions() {
    try {
      const value = JSON.parse(optionsEditor.value || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch { return null; }
  }

  function refreshPreview() {
    const target = readOptions()?.targetPlatform;
    const tokenPair = tokenInput.value.trim();
    const edgeName = nameInput.value.trim();
    preview.textContent = target?.os && target?.arch
      ? `当前目标：${target.os} · ${target.arch} / ${edgeName || "待填写 edge_name"} / ${tokenPair || "待填写 token_pair"}`
      : "请先选择上方的边端目标平台。";
  }

  function restoreInputs() {
    const identity = readOptions()?.edgeIdentity || {};
    tokenInput.value = identity.tokenPair || "";
    nameInput.value = identity.edgeName || "";
    refreshPreview();
  }

  function persistInputs() {
    const options = readOptions();
    if (!options) { refreshPreview(); return; }
    const tokenPair = tokenInput.value.trim();
    const edgeName = nameInput.value.trim();
    if (tokenPair || edgeName) options.edgeIdentity = {tokenPair, edgeName};
    else delete options.edgeIdentity;
    synchronizing = true;
    optionsEditor.value = JSON.stringify(options, null, 2);
    optionsEditor.dispatchEvent(new Event("input", {bubbles: true}));
    synchronizing = false;
    refreshPreview();
  }

  function refresh() {
    panel.classList.toggle("hidden", backend.value !== "nifi_native");
    if (backend.value === "nifi_native") restoreInputs();
  }

  backend.addEventListener("change", () => queueMicrotask(refresh));
  optionsEditor.addEventListener("input", () => {
    if (!synchronizing && backend.value === "nifi_native") restoreInputs();
  });
  tokenInput.addEventListener("input", persistInputs);
  nameInput.addEventListener("input", persistInputs);
  refresh();

  const originalCurrentOptions = window.currentOptions;
  window.currentOptions = function currentOptionsWithEdge() {
    const options = originalCurrentOptions();
    if (backend.value !== "nifi_native") return options;
    if (!options.targetPlatform?.os || !options.targetPlatform?.arch) {
      throw new Error("发布到 NiFi Native 前，请选择目标操作系统和 CPU 架构。");
    }
    const tokenPair = tokenInput.value.trim();
    const edgeName = nameInput.value.trim();
    if (!tokenPair || !edgeName) throw new Error("发布到 NiFi Native 前，请填写 token_pair 和 edge_name。");
    options.edgeIdentity = {tokenPair, edgeName};
    return options;
  };

  const publishResponse = document.getElementById("publishResponse");
  if (publishResponse) {
    new MutationObserver(() => {
      let payload;
      try { payload = JSON.parse(publishResponse.textContent || "{}"); }
      catch { return; }
      const identity = payload?.result?.edgeIdentity;
      if (!identity?.tokenPair || !identity?.edgeName) return;
      const summary = document.getElementById("publishSummary");
      if (!summary) return;
      let line = document.getElementById("edgePublishedIdentity");
      if (!line) {
        line = document.createElement("div");
        line.id = "edgePublishedIdentity";
        line.className = "edge-identity-meta";
        summary.after(line);
      }
      const outcome = payload.result.reusedExistingBundle ? "复用已有 Bundle（未重新打包）" : "生成新 Bundle";
      line.textContent = `${outcome}，目标机器：${identity.edgeName}（token_pair：${identity.tokenPair}）`;
    }).observe(publishResponse, {childList: true, subtree: true, characterData: true});
  }
})();
