"use strict";

/*
 * This browser only calls the BFF, which can call ONLY Publish Service.
 * Analyze structures and CreateVirtualContractRequest root fields are taken
 * from supplied source; nested model fields come from live /openapi.json.
 */

const $ = (id) => document.getElementById(id);
const S = globalThis.SchemaTools;
const Form = globalThis.FormBuilder;
const st = {
  step: 0, maxStep: 0,
  health: null, doc: null, schema: null, schemaKeys: null, schemas: null,
  analysis: null, selectedId: null, formFor: null,
  operatorForm: null, outputForm: null, additionalForm: null,
  constructorForms: new Map(), argumentForms: new Map(),
  reviewBody: null, created: null,
  backendOptions: {}, lastBackend: "",
};

const plain = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const pretty = (v) => JSON.stringify(v, null, 2);
const text = (id, value) => { $(id).textContent = value == null ? "—" : String(value); };
const node = (tag, css, content) => {
  const n = document.createElement(tag);
  if (css) n.className = css;
  if (content !== undefined && content !== null) n.textContent = String(content);
  return n;
};

function showNotice(message, kind = "") {
  $("noticeText").textContent = message;
  $("notice").className = kind ? `notice ${kind}` : "notice";
}
function hideNotice() { $("notice").classList.add("hidden"); }
$("dismissNotice").addEventListener("click", hideNotice);

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = $(button.dataset.copyTarget);
    const content = target?.textContent?.trim();
    if (!content) {
      showNotice("当前没有可以复制的内容。", "error");
      return;
    }
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(content);
      } else {
        const scratch = document.createElement("textarea");
        scratch.value = content;
        scratch.style.position = "fixed";
        scratch.style.opacity = "0";
        document.body.append(scratch);
        scratch.select();
        const copied = document.execCommand("copy");
        scratch.remove();
        if (!copied) throw new Error("浏览器未允许复制");
      }
      showNotice("已复制到剪贴板。", "success");
    } catch (error) {
      showNotice(`复制失败：${error.message}`, "error");
    }
  });
});

function errorText(response) {
  const body = response?.data;
  if (body?.detail !== undefined) {
    return typeof body.detail === "string" ? body.detail : pretty(body.detail);
  }
  return typeof body === "string" ? body : pretty(body);
}
async function api(method, path, body) {
  const options = {method, headers: {Accept: "application/json"}};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = pretty(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch (error) { throw new Error(`Web BFF 连接失败：${error.message}`); }
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : null; }
  catch { data = raw; }
  return {ok: response.ok, status: response.status, data};
}
let pendingActions = 0;
async function busy(id, action) {
  const button = $(id);
  if (button.disabled) return;
  button.disabled = true;
  button.classList.add("is-loading");
  button.setAttribute("aria-busy", "true");
  pendingActions++;
  $("globalProgress").classList.remove("hidden");
  try {
    await action();
  } catch (error) {
    showNotice(error.message || String(error), "error");
  } finally {
    pendingActions = Math.max(0, pendingActions - 1);
    $("globalProgress").classList.toggle("hidden", pendingActions === 0);
    button.classList.remove("is-loading");
    button.removeAttribute("aria-busy");
    button.disabled = id === "createBtn"
      ? (!st.reviewBody || !$("confirmReview").checked || !!st.created)
      : false;
  }
}
function safePart(value, name) {
  const part = String(value ?? "").trim();
  if (!part || part === "." || part === ".." ||
      part.length > 256 || /[\/\\%?#\r\n]/.test(part)) {
    throw new Error(`${name} 为空或包含无效路径字符`);
  }
  return encodeURIComponent(part);
}
function navigate(index) {
  if (index > st.maxStep) return;
  st.step = index;
  for (let i = 0; i <= 4; i++) {
    $(`step${i}`).classList.toggle("hidden", i !== index);
    const button = document.querySelector(`[data-step="${i}"]`);
    button.disabled = i > st.maxStep;
    button.classList.toggle("active", i === index);
    button.classList.toggle("done", i < index && i <= st.maxStep);
    if (i === index) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  }
  hideNotice();
  window.scrollTo?.({top: 0, behavior: "instant"});
  $(`step${index}`).querySelector("h1")?.focus({preventScroll: true});
}
function setAccessible(index) {
  st.maxStep = index;
  navigate(Math.min(st.step, index));
}
document.querySelectorAll("[data-step]").forEach((button) => {
  button.addEventListener("click", () => navigate(Number(button.dataset.step)));
});

/* OpenAPI schema: fields of OperatorSelection / BindingSelection /
   OutputSelection are never hardcoded or inferred from conversation history. */
function prepareSchema(doc) {
  const schema = S.requestSchema(doc, "/v1/operators");
  if (!schema) throw new Error(
    "无法读取算子创建配置，请联系管理员检查服务接口。"
  );
  const model = S.flatten(schema, doc);
  const mandatory = [
    "workspace", "source_revision", "python_root", "operator",
    "callable_id", "constructor_bindings", "argument_bindings", "output",
  ];
  const keys = {};
  for (const logical of mandatory) {
    const actual = S.propertyName(schema, doc, logical);
    if (!actual) {
      throw new Error(`算子创建配置不完整（${logical}），请联系管理员。`);
    }
    keys[logical] = actual;
  }
  const operator = model.properties[keys.operator];
  const output = model.properties[keys.output];
  const ctorDict = model.properties[keys.constructor_bindings];
  const argDict = model.properties[keys.argument_bindings];
  const findNamedModel = (name) => {
    const components = doc?.components?.schemas || {};
    return components[name] || Object.entries(components).find(
      ([key]) => key.endsWith(name)
    )?.[1] || null;
  };
  const constructorBinding = S.dictionaryValueSchema(ctorDict, doc)
    || findNamedModel("BindingSelection");
  const argumentBinding = S.dictionaryValueSchema(argDict, doc)
    || findNamedModel("BindingSelection");
  if (!operator || !output || !constructorBinding || !argumentBinding) {
    throw new Error(
      "契约配置暂时不可用，请联系管理员检查服务配置。"
    );
  }
  const extras = Object.fromEntries(Object.entries(model.properties || {}).filter(
    ([key]) => !Object.values(keys).includes(key)
  ));
  const extraRequired = (model.required || []).filter((key) =>
    Object.prototype.hasOwnProperty.call(extras, key)
  );
  return {
    schema, keys,
    operator, output,
    constructorBinding, argumentBinding,
    extras: {type: "object", properties: extras, required: extraRequired},
  };
}

async function loadSchema() {
  const response = await api("GET", "/api/openapi");
  if (!response.ok) {
    throw new Error(`/api/openapi 返回 HTTP ${response.status}: ${errorText(response)}`);
  }
  const resolved = prepareSchema(response.data);
  st.doc = response.data;
  st.schema = resolved.schema;
  st.schemaKeys = resolved.keys;
  st.schemas = resolved;
  $("schemaUnavailable").classList.add("hidden");
  $("reloadSchemaBtn").classList.add("hidden");
  if (st.selectedId && st.step === 2) {
    buildContractForm();
  }
}

async function schemaOrWarning() {
  try { await loadSchema(); }
  catch (error) {
    st.doc = st.schema = st.schemas = st.schemaKeys = null;
    $("schemaUnavailable").textContent =
      `暂时无法加载契约配置：${error.message}\n` +
      "请检查服务连接，或联系管理员后重试。";
    $("schemaUnavailable").classList.remove("hidden");
    $("reloadSchemaBtn").classList.remove("hidden");
    showNotice("暂时无法加载契约配置，请重试或联系管理员。", "error");
  }
}
$("reloadSchemaBtn").addEventListener("click", () =>
  busy("reloadSchemaBtn", async () => {
    await schemaOrWarning();
    if (st.schemas && st.selectedId) buildContractForm();
  })
);

/* 1. Analyze. Workspace is a relative path *inside* Publish Service's
   configured workspace_root; never interpreted on the browser machine. */
function clearAfterWorkspaceChange({resetCandidates = false} = {}) {
  st.analysis = null; st.selectedId = null;
  st.formFor = null; st.reviewBody = null; st.created = null;
  st.backendOptions = {};
  st.lastBackend = "";
  st.constructorForms.clear(); st.argumentForms.clear();
  $("analyzeSummary").classList.add("hidden");
  $("contextStrip").classList.add("hidden");
  $("contextCallableItem").classList.add("hidden");
  $("catalogCount").textContent = "";
  $("callableSearch").value = "";
  $("showUnsupported").checked = false;
  $("callableCatalog").replaceChildren();
  $("callableDetails").classList.add("hidden");
  $("compilePanel").classList.add("hidden");
  $("publishPanel").classList.add("hidden");
  if (resetCandidates) {
    const select = $("requestedRoot");
    select.replaceChildren();
    const automatic = node("option", "", "自动推荐（首次分析）");
    automatic.value = "";
    select.append(automatic);
  }
  setAccessible(0);
}
function updateWorkspacePreview() {
  const path = $("workspace").value.trim();
  const unsafe = path.startsWith("/") ||
    path.split(/[\\/]/).some((part) => part === "..");
  text("workspacePreview", path && !unsafe
    ? `/data/user-workspace/${path}`
    : path ? "路径包含无效片段，请修正后再分析" : "/data/user-workspace/…");
}
$("workspace").addEventListener("input", () => {
  updateWorkspacePreview();
  clearAfterWorkspaceChange({resetCandidates: true});
});
$("requestedRoot").addEventListener("change", () =>
  clearAfterWorkspaceChange()
);
updateWorkspacePreview();

function validateAnalyze(data) {
  if (!plain(data) || !Array.isArray(data.callables) ||
      typeof data.sourceRevision !== "string" || !data.sourceRevision ||
      typeof data.pythonRoot !== "string" ||
      typeof data.workspace !== "string" ||
      !Array.isArray(data.pythonRootCandidates)) {
    throw new Error(
      "Analyze 响应与所提供的 PublishService.analyze() 结构不一致；" +
      "已停止后续步骤，避免生成错误契约。"
    );
  }
  return data;
}

function rootChoices(analysis) {
  const select = $("requestedRoot");
  select.replaceChildren();
  for (const item of analysis.pythonRootCandidates) {
    const option = node("option", "",
      `${item.path === "." ? ". (项目根目录)" : item.path}` +
      (item.recommended ? " · 推荐" : "")
    );
    option.value = String(item.path);
    select.append(option);
  }
  select.value = analysis.pythonRoot;
  if (select.value !== analysis.pythonRoot) {
    const option = node("option", "", analysis.pythonRoot);
    option.value = analysis.pythonRoot;
    select.append(option);
    select.value = analysis.pythonRoot;
  }
}

function analyzeWarnings(values) {
  const warning = $("warningBox");
  warning.replaceChildren();
  if (!Array.isArray(values) || !values.length) {
    warning.classList.add("hidden");
    return;
  }
  warning.append(node("strong", "", `扫描警告（${values.length}）`));
  const list = node("ul");
  for (const item of values) {
    list.append(node("li", "", typeof item === "string" ? item : pretty(item)));
  }
  warning.append(list);
  warning.classList.remove("hidden");
}

async function analyze() {
  const workspace = $("workspace").value.trim();
  if (!workspace || workspace.startsWith("/")) {
    throw new Error("Workspace 必须是 /data/user-workspace 下的非空相对路径");
  }
  const requestedRoot = $("requestedRoot").value;
  // Clear previous data BEFORE the request, so a failure cannot reuse a
  // stale sourceRevision or an older Callable selection.
  clearAfterWorkspaceChange();
  const payload = {workspace};
  if (requestedRoot) payload.python_root = requestedRoot;
  const response = await api("POST", "/api/authoring/analyze", payload);
  if (!response.ok) {
    throw new Error(`Analyze HTTP ${response.status}：${errorText(response)}`);
  }
  const analysis = validateAnalyze(response.data);
  st.analysis = analysis;
  rootChoices(analysis);
  text("contextWorkspace", analysis.workspace);
  text("contextRoot", analysis.pythonRoot);
  $("contextStrip").classList.remove("hidden");
  text("analyzedWorkspace", analysis.workspace);
  text("analyzedRoot", analysis.pythonRoot);
  text("revision", analysis.sourceRevision);
  text("callableCount",
    `${analysis.callables.filter((c) => c.supported).length} supported / ${analysis.callables.length} total`);
  $("analyzeRaw").textContent = pretty(analysis);
  analyzeWarnings(analysis.warnings);
  $("analyzeSummary").classList.remove("hidden");
  st.maxStep = 1;
  navigate(0);
  if (analysis.callables.every((item) => !item.supported)) {
    $("toCallablesBtn").disabled = true;
    showNotice("Analyze 完成，但没有 supported Callable。请检查原始响应中的 unsupportedReasons。", "error");
  } else {
    $("toCallablesBtn").disabled = false;
    showNotice(
      `Analyze 成功：使用 Python Root「${analysis.pythonRoot}」；Source Revision 已固定。`,
      "success"
    );
  }
}
$("analyzeBtn").addEventListener("click", () => busy("analyzeBtn", analyze));
$("toCallablesBtn").addEventListener("click", () => {
  if (!st.analysis) return;
  renderCatalog();
  navigate(1);
});
$("backWorkspaceBtn").addEventListener("click", () => navigate(0));

/* 2. Callable selection. Always respect supported / unsupportedReasons. */
function currentCallable() {
  return st.analysis?.callables?.find((item) => item.id === st.selectedId) || null;
}
function parameterTable(items) {
  if (!items?.length) return node("p", "muted", "无参数");
  const table = node("table", "parameter-table");
  const head = node("thead");
  const headRow = node("tr");
  ["参数", "类型", "必填", "允许来源"].forEach((label) =>
    headRow.append(node("th", "", label))
  );
  head.append(headRow);
  table.append(head);
  const body = node("tbody");
  for (const param of items) {
    const row = node("tr");
    [
      param.name,
      param.annotation || param.kind || "—",
      param.required ? "是" : "否",
      Array.isArray(param.allowedSources) ? param.allowedSources.join(", ") : "—",
    ].forEach((value) => row.append(node("td", "", value)));
    body.append(row);
  }
  table.append(body);
  return table;
}
function renderCallableDetails(item) {
  const host = $("callableDetails");
  host.replaceChildren();
  if (!item) { host.classList.add("hidden"); return; }
  host.append(node("h3", "", item.displayName || item.id));
  host.append(node("p", "muted",
    `${item.kind || ""} · ${item.file || ""}:${item.line ?? ""}`));
  host.append(node("p", "muted", `Return: ${item.returnAnnotation ?? "未标注"}`));
  host.append(node("h3", "", "构造参数"));
  host.append(parameterTable(item.constructorParameters));
  host.append(node("h3", "", "调用参数"));
  host.append(parameterTable(item.parameters));
  host.classList.remove("hidden");
}

function chooseCallable(id) {
  const item = st.analysis?.callables?.find((candidate) => candidate.id === id);
  if (!item || !item.supported) return;
  if (st.selectedId !== id) {
    st.formFor = null;
    st.reviewBody = null;
    st.created = null;
    st.backendOptions = {};
    st.lastBackend = "";
    st.maxStep = 1;
  }
  st.selectedId = id;
  text("contextCallable", item.displayName || item.id);
  $("contextCallableItem").classList.remove("hidden");
  $("callableCatalog").querySelectorAll(".callable-option").forEach((row) => {
    row.classList.toggle("selected", row.dataset.callableId === id);
    row.querySelector("input").checked = row.dataset.callableId === id;
  });
  renderCallableDetails(item);
}

function renderCatalog() {
  const catalog = $("callableCatalog");
  catalog.replaceChildren();
  const info = st.analysis;
  if (!info) return;

  const search = $("callableSearch").value.trim().toLocaleLowerCase();
  const showUnsupported = $("showUnsupported").checked;
  const matching = info.callables.filter((item) => {
    if (!showUnsupported && !item.supported) return false;
    if (!search) return true;
    const haystack = [
      item.id, item.displayName, item.kind, item.file,
      ...(Array.isArray(item.unsupportedReasons) ? item.unsupportedReasons : []),
    ].map((value) => typeof value === "string" ? value : pretty(value))
      .join(" ").toLocaleLowerCase();
    return haystack.includes(search);
  });
  const supportedCount = info.callables.filter((item) => item.supported).length;
  text("catalogCount",
    `显示 ${matching.length} 个 · ${supportedCount} 个可用 / 共 ${info.callables.length} 个入口`);

  for (const item of matching) {
    const label = node("label",
      `callable-option${item.supported ? "" : " unsupported"}`);
    label.dataset.callableId = item.id;
    const radio = node("input");
    radio.type = "radio";
    radio.name = "callable";
    radio.disabled = !item.supported;
    radio.value = item.id;
    label.append(radio);
    const content = node("div");
    content.append(node("div", "callable-title", item.displayName || item.id));
    content.append(node("div", "callable-meta",
      `${item.kind || ""} · ${item.file || ""}:${item.line ?? ""}`));
    content.append(node("span",
      item.supported ? "badge" : "badge bad",
      item.supported ? "可用于发布" : "暂不可用"));
    if (item.id === info.recommendedCallableId && item.supported) {
      content.append(node("span", "badge recommended", "推荐入口"));
    }
    if (!item.supported && item.unsupportedReasons?.length) {
      content.append(node("p", "muted",
        item.unsupportedReasons.map((reason) =>
          typeof reason === "string" ? reason : pretty(reason)
        ).join("；")));
    }
    label.append(content);
    if (item.supported) {
      label.addEventListener("click", () => chooseCallable(item.id));
    }
    catalog.append(label);
  }
  if (!matching.length) {
    catalog.append(node("div", "empty-state",
      search ? "没有匹配的入口。试试函数名或文件名，或清除搜索条件。"
        : "当前筛选下没有入口。可以开启“显示不可用入口”查看原因。"));
  }

  const suggested = info.callables.find((item) =>
    item.id === info.recommendedCallableId && item.supported);
  const first = info.callables.find((item) => item.supported);
  const wanted = info.callables.find((item) =>
    item.id === st.selectedId && item.supported);
  if (wanted || suggested || first) chooseCallable((wanted || suggested || first).id);
}
$("callableSearch").addEventListener("input", renderCatalog);
$("showUnsupported").addEventListener("change", renderCatalog);

$("toBindingsBtn").addEventListener("click", () => {
  if (!currentCallable()?.supported) {
    showNotice("请选择一个 Supported Callable。", "error");
    return;
  }
  st.maxStep = Math.max(st.maxStep, 2);
  navigate(2);
  if (st.schemas) buildContractForm();
  else schemaOrWarning().then(() => {
    if (st.schemas) buildContractForm();
  });
});


/* 3. Contract configuration. Dynamic nested fields are read from OpenAPI. */
function clearContainer(id) { $(id).replaceChildren(); }

function bindingSchemaFor(group) {
  return group === "constructor" ?
    st.schemas.constructorBinding : st.schemas.argumentBinding;
}
function safeParam(param) {
  return plain(param) && typeof param.name === "string" && param.name.length > 0;
}

function updateContractProgress() {
  const entries = [
    ...st.constructorForms.values(), ...st.argumentForms.values(),
  ];
  const required = entries.filter((entry) => entry.param.required).length;
  let filled = 0;
  for (const entry of entries) {
    if (!entry.checkbox.checked) continue;
    try {
      if (plain(entry.read())) filled++;
    } catch {
      // A required source or subfield has not been selected yet.
    }
  }
  const total = entries.length;
  text("contractProgress",
    total ? `已填写 ${filled}/${total} 项 · 必填 ${required} 项` : "此入口没有参数绑定");
  $("contractProgressBar").style.width =
    `${total ? Math.round(filled * 100 / total) : 100}%`;
  text("constructorCount", `${st.constructorForms.size} 项`);
  text("argumentCount", `${st.argumentForms.size} 项`);
}

function renderBindingRows(containerId, group, parameters) {
  const host = $(containerId);
  host.replaceChildren();
  const records = group === "constructor"
    ? st.constructorForms : st.argumentForms;
  records.clear();
  if (!parameters?.length) {
    host.append(node("div", "empty-state",
      group === "constructor" ? "此入口不需要构造参数。" : "此入口没有调用参数。"));
    return;
  }

  const schema = bindingSchemaFor(group);
  for (const param of parameters) {
    if (!safeParam(param)) {
      throw new Error("参数信息缺少有效名称，无法生成配置。");
    }
    if (records.has(param.name)) {
      throw new Error(`参数名重复：${param.name}`);
    }
    const allowed = Array.isArray(param.allowedSources)
      ? param.allowedSources.filter((item) => typeof item === "string")
      : [];
    const box = node("div", `bound-parameter${param.required ? " is-enabled" : ""}`);
    const heading = node("div", "binding-header");
    const titles = node("div");
    titles.append(node("h3", "", param.name));
    const information = [
      param.annotation ? `类型 ${param.annotation}` : "",
      param.required ? "必填参数" : "可选参数",
      param.hasLiteralDefault ? `默认值 ${pretty(param.defaultValue)}` : "",
    ].filter(Boolean);
    titles.append(node("div", "parameter-info", information.join(" · ")));
    heading.append(titles);

    const toggleLabel = node("label", "inline-check");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.checked = !!param.required;
    checkbox.disabled = !!param.required;
    toggleLabel.append(checkbox);
    toggleLabel.append(node("span", "",
      param.required ? "必填绑定" : "启用绑定"));
    heading.append(toggleLabel);
    box.append(heading);

    const bindingHost = node("div", "binding-editor");
    const controls = node("div");
    bindingHost.append(controls);
    let current = null;

    function install(value) {
      controls.replaceChildren();
      current = BindingEditor.makeEditor({
        schema,
        doc: st.doc,
        parameterName: param.name,
        allowedSources: allowed,
        initial: value,
        onUpdate: updateContractProgress,
      });
      controls.append(current.element);
    }
    install(undefined);

    const suggestions = Array.isArray(param.suggestions) ? param.suggestions : [];
    if (suggestions.length) {
      const suggestionsPanel = node("details", "suggestion-list");
      suggestionsPanel.append(node("summary", "",
        `推荐配置（${suggestions.length}）`));
      for (const suggestion of suggestions) {
        const line = node("div", "suggestion");
        const body = node("div");
        const proposed = BindingEditor.fromSuggestion(
          suggestion, schema, st.doc, allowed
        );
        const sourceKey = S.sourceField(schema, st.doc, allowed);
        const source = sourceKey && proposed ? proposed[sourceKey] : null;
        body.append(node("strong", "suggestion-name", source
          ? `使用${BindingEditor.displaySource(source).title}` : "推荐方案"));
        const detail = node("details", "suggestion-raw");
        detail.append(node("summary", "", "查看配置详情"));
        detail.append(node("pre", "", pretty(suggestion)));
        body.append(detail);
        line.append(body);
        if (proposed) {
          const apply = node("button", "button secondary small", "应用");
          apply.type = "button";
          apply.addEventListener("click", () => {
            let selection = {...proposed};
            // A partial suggestion may omit the source. Preserve a
            // previously confirmed source if possible, never invent one.
            if (sourceKey && !selection[sourceKey] && current) {
              const selected = current.getSource();
              if (selected) selection[sourceKey] = selected;
            }
            install(selection);
            checkbox.checked = true;
            bindingHost.classList.remove("hidden");
            box.classList.add("is-enabled");
            updateContractProgress();
          });
          line.append(apply);
        } else {
          line.append(node("small", "", "可参考此方案手动配置"));
        }
        suggestionsPanel.append(line);
      }
      bindingHost.append(suggestionsPanel);
    }
    box.append(bindingHost);
    checkbox.addEventListener("change", () => {
      bindingHost.classList.toggle("hidden", !checkbox.checked);
      box.classList.toggle("is-enabled", checkbox.checked);
      updateContractProgress();
    });
    bindingHost.addEventListener("change", updateContractProgress);
    bindingHost.addEventListener("input", updateContractProgress);
    bindingHost.classList.toggle("hidden", !checkbox.checked);
    host.append(box);
    records.set(param.name, {
      param,
      checkbox,
      read: () => current.read(),
    });
  }
}

function extraEditor() {
  const extraProps = st.schemas?.extras?.properties || {};
  const names = Object.keys(extraProps);
  const host = $("additionalPanel");
  clearContainer("additionalEditor");
  if (!names.length) {
    host.classList.add("hidden");
    st.additionalForm = null;
    return;
  }
  host.classList.remove("hidden");
  st.additionalForm = Form.editor(st.schemas.extras, {
    doc: st.doc, name: "Additional CreateVirtualContractRequest fields",
  });
  $("additionalEditor").append(st.additionalForm.element);
}

function outputCodec(output) {
  if (!plain(output)) return null;
  const payload = output.payload || output.outputPayload || output.output_payload;
  if (plain(payload)) {
    return payload.codec || payload.payloadCodec || payload.payload_codec || null;
  }
  return output.payloadCodec || output.payload_codec || output.codec || null;
}

function outputCodecGuidance() {
  if (!st.outputForm) return "";
  let output;
  try { output = st.outputForm.read(); }
  catch { return ""; }
  const codec = outputCodec(output);
  if (typeof codec !== "string" || codec.toLowerCase() !== "bytes") return "";
  const annotation = String(currentCallable()?.returnAnnotation || "");
  const structured = /(^|[^a-z])(dict|list|tuple|set|mapping|sequence|int|float|bool|none|nonetype)([^a-z]|$)/i.test(annotation);
  return structured
    ? `函数声明的返回类型为 ${annotation}。当前选择“原始字节”，只能直接处理字节或文本；请确认实际返回值类型，必要时更换支持该类型的输出格式。`
    : "原始字节格式只能直接处理 bytes、bytearray 或字符串。如果函数返回对象、列表、数字或空值，请选择与返回值匹配的输出格式。";
}

function refreshOutputGuidance() {
  const annotation = String(currentCallable()?.returnAnnotation || "");
  const structured = /(^|[^a-z])(dict|list|tuple|set|mapping|sequence)([^a-z]|$)/i.test(annotation);
  const hint = $("outputTypeHint");
  hint.textContent = structured
    ? `此入口声明返回 ${annotation}。请确认输出格式能够处理对象或列表。`
    : "";
  hint.classList.toggle("hidden", !structured);
  const message = outputCodecGuidance();
  for (const id of ["outputTypeWarning", "reviewOutputWarning"]) {
    const box = $(id);
    box.textContent = message;
    box.classList.toggle("hidden", !message);
  }
}

$("outputEditor").addEventListener("input", refreshOutputGuidance);
$("outputEditor").addEventListener("change", refreshOutputGuidance);

function buildContractForm() {
  if (!st.analysis || !currentCallable()?.supported || !st.schemas) return;
  if (st.formFor === st.selectedId && st.operatorForm) return;
  const item = currentCallable();
  st.reviewBody = null;
  st.created = null;
  st.formFor = null;
  text("selectedCallableBadge", item.displayName || item.id);
  const hasConstructor = !!item.constructorParameters?.length;
  $("constructorPanel").classList.toggle("hidden", !hasConstructor);
  $("constructorJump").classList.toggle("hidden", !hasConstructor);
  st.operatorForm = Form.editor(st.schemas.operator, {
    doc: st.doc, name: "OperatorSelection",
  });
  clearContainer("operatorEditor");
  $("operatorEditor").append(st.operatorForm.element);

  renderBindingRows(
    "constructorBindings", "constructor", item.constructorParameters || []
  );
  renderBindingRows(
    "argumentBindings", "argument", item.parameters || []
  );

  st.outputForm = Form.editor(st.schemas.output, {
    doc: st.doc, name: "OutputSelection",
  });
  clearContainer("outputEditor");
  $("outputEditor").append(st.outputForm.element);
  refreshOutputGuidance();
  extraEditor();
  st.formFor = st.selectedId;
  updateContractProgress();
}

function collectBindings(records, label) {
  const collected = {};
  for (const [name, entry] of records.entries()) {
    if (!entry.checkbox.checked) {
      if (entry.param.required) {
        throw new Error(`必填${label}「${name}」尚未启用绑定`);
      }
      continue;
    }
    const selection = entry.read();
    if (!plain(selection)) {
      throw new Error(`${label}「${name}」的 BindingSelection 必须是 JSON object`);
    }
    collected[name] = selection;
  }
  return collected;
}

function composeCreateRequest() {
  const analysis = st.analysis;
  const callable = currentCallable();
  if (!analysis || !callable?.supported) {
    throw new Error("Analyze 或 Callable 未确认");
  }
  if (!st.schemas || st.formFor !== st.selectedId) {
    throw new Error("契约表单尚未准备好，请重新打开当前步骤。");
  }
  if ($("workspace").value.trim() !== analysis.workspace ||
      $("requestedRoot").value !== analysis.pythonRoot) {
    throw new Error("Workspace 或 Python Root 已变化，请重新 Analyze");
  }
  const operator = st.operatorForm.read();
  const output = st.outputForm.read();
  if (!plain(operator) || !plain(output)) {
    throw new Error("OperatorSelection 和 OutputSelection 都必须是对象");
  }
  const ctor = collectBindings(st.constructorForms, "构造参数");
  const argumentsMap = collectBindings(st.argumentForms, "调用参数");
  const keys = st.schemaKeys;
  const payload = {
    [keys.workspace]: analysis.workspace,
    [keys.source_revision]: analysis.sourceRevision,
    [keys.python_root]: analysis.pythonRoot,
    [keys.operator]: operator,
    [keys.callable_id]: callable.id,
    [keys.constructor_bindings]: ctor,
    [keys.argument_bindings]: argumentsMap,
    [keys.output]: output,
  };
  if (st.additionalForm) {
    const additional = st.additionalForm.read();
    if (!plain(additional)) throw new Error("附加请求字段必须为 object");
    for (const [key, value] of Object.entries(additional)) {
      if (Object.prototype.hasOwnProperty.call(payload, key)) {
        throw new Error(`重复请求字段：${key}`);
      }
      payload[key] = value;
    }
  }
  return payload;
}

function addSummary(container, title, value) {
  const card = node("div", "summary-item");
  card.append(node("small", "", title));
  card.append(node("strong", "", value == null ? "—" : String(value)));
  container.append(card);
}

$("backCallablesBtn").addEventListener("click", () => navigate(1));
$("asideReviewBtn").addEventListener("click", () => $("reviewBtn").click());
$("reviewBtn").addEventListener("click", () => {
  try {
    const body = composeCreateRequest();
    st.reviewBody = body;
    text("reviewWorkspace", st.analysis.workspace);
    text("reviewCallable", currentCallable().displayName || st.selectedId);
    text("reviewRevision", st.analysis.sourceRevision);
    $("requestPreview").textContent = pretty(body);
    refreshOutputGuidance();
    const summary = $("reviewCounts");
    summary.replaceChildren();
    addSummary(summary, "构造参数", `${Object.keys(body[st.schemaKeys.constructor_bindings]).length} 项绑定`);
    addSummary(summary, "调用参数", `${Object.keys(body[st.schemaKeys.argument_bindings]).length} 项绑定`);
    const output = body[st.schemaKeys.output];
    addSummary(summary, "输出配置",
      Object.keys(output).length ? `${Object.keys(output).length} 个字段已设置` : "未显式设置（使用服务端默认）");
    addSummary(summary, "入口类型", currentCallable().kind || "—");

    const bindingList = $("reviewBindings");
    bindingList.replaceChildren();
    bindingList.append(node("h3", "", "参数绑定明细"));
    let rows = 0;
    for (const [name, key, schema] of [
      ["构造参数", st.schemaKeys.constructor_bindings, st.schemas.constructorBinding],
      ["调用参数", st.schemaKeys.argument_bindings, st.schemas.argumentBinding],
    ]) {
      for (const [parameterName, binding] of Object.entries(body[key])) {
        const row = node("div", "review-binding-row");
        row.append(node("span", "", `${name} · ${parameterName}`));
        const sourceField = S.sourceField(schema, st.doc,
          name === "构造参数"
            ? ["operator.parameter", "constant"]
            : ["input.payload", "input.metadata", "operator.parameter", "constant"]);
        row.append(node("code", "",
          sourceField && binding[sourceField] !== undefined
            ? String(binding[sourceField]) : "已配置（见请求详情）"));
        bindingList.append(row);
        rows++;
      }
    }
    if (!rows) {
      bindingList.append(node("p", "muted", "没有显式配置的参数绑定。"));
    }
    $("overrideRequest").checked = false;
    $("overrideBody").disabled = true;
    $("overrideBody").value = pretty(body);
    $("confirmReview").checked = false;
    $("createBtn").disabled = true;
    $("createError").classList.add("hidden");
    st.maxStep = Math.max(st.maxStep, 3);
    navigate(3);
  } catch (error) {
    showNotice(error.message || String(error), "error");
  }
});

$("editContractBtn").addEventListener("click", () => {
  st.reviewBody = null;
  $("confirmReview").checked = false;
  $("createBtn").disabled = true;
  st.maxStep = 2;
  navigate(2);
});

function resetApproval() {
  $("confirmReview").checked = false;
  $("createBtn").disabled = true;
}
$("confirmReview").addEventListener("change", () => {
  $("createBtn").disabled = !$("confirmReview").checked || !st.reviewBody;
});
$("overrideRequest").addEventListener("change", () => {
  const enabled = $("overrideRequest").checked;
  $("overrideBody").disabled = !enabled;
  $("requestPreview").textContent = enabled
    ? $("overrideBody").value : pretty(st.reviewBody);
  resetApproval();
});
$("overrideBody").addEventListener("input", () => {
  if ($("overrideRequest").checked) {
    $("requestPreview").textContent = $("overrideBody").value;
    resetApproval();
  }
});

async function createContract() {
  if (!st.reviewBody || !$("confirmReview").checked) {
    throw new Error("请先核对并确认 Virtual Contract");
  }
  let body = st.reviewBody;
  if ($("overrideRequest").checked) {
    try { body = JSON.parse($("overrideBody").value); }
    catch (error) { throw new Error(`覆盖请求 JSON 无效：${error.message}`); }
    if (!plain(body)) throw new Error("覆盖请求必须是 JSON object");
  }
  const result = await api("POST", "/api/operators", body);
  if (!result.ok) {
    const detail = errorText(result);
    $("createError").textContent =
      `Publish Service HTTP ${result.status}\n${detail}\n` +
      `完整响应：\n${pretty(result.data)}`;
    $("createError").classList.remove("hidden");
    if (detail.includes("SOURCE_CHANGED")) {
      st.reviewBody = null;
      st.created = null;
      clearAfterWorkspaceChange();
      navigate(0);
      showNotice(
        "工作区中的代码已发生变化。请重新分析源代码，再确认入口与参数配置。",
        "error"
      );
    } else {
      showNotice(`创建契约失败：HTTP ${result.status}。下方保留真实服务错误。`, "error");
    }
    return;
  }
  // These names come directly from the supplied create_virtual_contract().
  const data = result.data;
  if (!plain(data) || typeof data.operatorId !== "string" || !data.operatorId) {
    $("createError").textContent =
      "Create 返回了成功 HTTP 状态，但没有提供所给源码定义的 operatorId。" +
      `\n真实响应：\n${pretty(data)}`;
    $("createError").classList.remove("hidden");
    showNotice("已收到成功响应，但无法定位 Operator ID；不能自行编造 ID。", "error");
    return;
  }
  st.created = data;
  text("createdOperatorId", data.operatorId);
  text("createdContractId", data.contractId);
  text("createdVersion", data.contractVersion);
  text("createdSourceRef", data.sourceRef);
  $("createResponse").textContent = pretty(data);
  st.maxStep = 4;
  renderBackends();
  navigate(4);
  showNotice(
    `Virtual Contract ${data.contractId || ""} 已保存；不可变源引用：${data.sourceRef || "见原始响应"}`,
    "success"
  );
}
$("createBtn").addEventListener("click", () => busy("createBtn", createContract));


/* 5. Compile + Publish, both delegated to Publish Service only. */
function chosenBackend() {
  const value = $("backend").value;
  if (!value) throw new Error("请选择 backend");
  return safePart(value, "backend");
}
function currentOptions() {
  let value;
  try { value = JSON.parse($("options").value); }
  catch (error) { throw new Error(`options JSON 无法解析：${error.message}`); }
  if (!plain(value)) {
    throw new Error("BackendRequest.options 必须是 JSON object (dict[str, Any])");
  }
  return value;
}
function describeBackend(backend) {
  let description;
  if (backend === "runner") {
    description =
      "发布 Runner Release，运行时环境由 Publish Service 内部准备。" +
      "如果环境尚未 READY，页面会显示服务端返回的具体原因。";
  } else if (backend === "nifi_native") {
    description =
      "生成 NiFi Python 扩展包。生成成功不代表已经部署；" +
      "如果响应要求部署，请按返回的路径和说明完成安装。";
  } else {
    description = "使用 Publish Service 声明的执行后端。";
  }
  text("backendDescription", description);
}

function syncProfileFromOptions() {
  const runner = $("backend").value === "runner";
  $("quickProfileField").classList.toggle("hidden", !runner);
  if (!runner) return;
  try {
    const values = JSON.parse($("options").value);
    $("runnerProfile").value =
      plain(values) && typeof values.profile === "string" ? values.profile : "";
  } catch {
    // Preserve user input; invalid JSON is reported on Compile / Publish.
    $("runnerProfile").value = "";
  }
}

function renderBackends() {
  const select = $("backend");
  const available = Array.isArray(st.health?.backends)
    ? st.health.backends.filter((name) => typeof name === "string" && name)
    : [];
  // The user-supplied /health and _compile_backend source confirms both names.
  const supported = available.length ? available : ["runner", "nifi_native"];
  select.replaceChildren();
  for (const backend of supported) {
    const option = node("option", "", backend === "runner"
      ? "Runner (runner)"
      : backend === "nifi_native" ? "NiFi Native (nifi_native)" : backend);
    option.value = backend;
    select.append(option);
  }
  st.lastBackend = select.value;
  $("options").value = st.backendOptions[select.value] || "{}";
  syncProfileFromOptions();
  describeBackend(select.value);
}

$("backend").addEventListener("change", () => {
  if (st.lastBackend) st.backendOptions[st.lastBackend] = $("options").value;
  st.lastBackend = $("backend").value;
  $("options").value = st.backendOptions[st.lastBackend] || "{}";
  $("compilePanel").classList.add("hidden");
  $("publishPanel").classList.add("hidden");
  syncProfileFromOptions();
  describeBackend(st.lastBackend);
});

$("runnerProfile").addEventListener("input", () => {
  if ($("backend").value !== "runner") return;
  let options;
  try {
    options = JSON.parse($("options").value);
    if (!plain(options)) throw new Error("options 必须是 JSON object");
  } catch {
    $("advancedOptions").open = true;
    showNotice("高级 options 中的 JSON 不正确，无法同步 Runner Profile。请先修复 JSON。", "error");
    return;
  }
  const profile = $("runnerProfile").value.trim();
  if (profile) options.profile = profile;
  else delete options.profile;
  $("options").value = pretty(options);
  st.backendOptions.runner = $("options").value;
});

$("options").addEventListener("input", () => {
  syncProfileFromOptions();
  if (st.lastBackend) st.backendOptions[st.lastBackend] = $("options").value;
});

function summary(containerId, records) {
  const target = $(containerId);
  target.replaceChildren();
  for (const [name, value] of records) {
    if (value !== undefined && value !== null) addSummary(target, name, value);
  }
}
async function backendAction(action) {
  if (!st.created?.operatorId) throw new Error("请先创建 Virtual Contract");
  const operatorId = safePart(st.created.operatorId, "operatorId");
  const backend = chosenBackend();
  const options = currentOptions();
  const path = `/api/operators/${operatorId}/backends/${backend}/${action}`;
  const response = await api("POST", path, {options});
  if (action === "compile") {
    $("compilePanel").classList.remove("hidden");
    $("compileResponse").textContent = pretty(response.data);
    summary("compileSummary", response.ok
      ? [
          ["Status", response.data?.status],
          ["Backend", response.data?.backend],
          ["Variant ID", response.data?.variantId],
          ["Variant Key", response.data?.variantKey],
          ["Parent Contract Version", response.data?.parentContractVersion],
          ["Backend Contract SHA256", response.data?.backendContractSha256],
        ]
      : [["HTTP Status", response.status], ["Error", errorText(response)]]);
  } else {
    $("publishPanel").classList.remove("hidden");
    $("publishResponse").textContent = pretty(response.data);
    summary("publishSummary", response.ok
      ? [
          ["Job ID", response.data?.jobId],
          ["Status", response.data?.status],
          ["Backend", response.data?.backend],
          ["Artifact Ref", response.data?.artifactRef],
          ["Runner Release ID", response.data?.result?.releaseId],
          ["Runtime Environment Key", response.data?.result?.envKey],
          ["Native Processor Type", response.data?.result?.processorType],
          ["Native Artifact File", response.data?.result?.artifactFile],
        ]
      : [["HTTP Status", response.status], ["Error", errorText(response)]]);
    const native = $("nativeNotice");
    const result = response.data?.result;
    if (response.ok && (result?.deploymentRequired === true)) {
      native.textContent = (
        "Native package 已生成，但尚未部署到 NiFi。\n" +
        `artifactFile: ${result.artifactFile || "见真实响应"}\n` +
        `deploymentHint: ${result.deploymentHint || "请检查部署流程"}`
      );
      native.classList.remove("hidden");
    } else {
      native.classList.add("hidden");
    }
  }
  const panel = $(action === "compile" ? "compilePanel" : "publishPanel");
  const status = response.data?.status;
  const upstreamFailure = typeof status === "string" && /^(failed|error)$/i.test(status);
  const success = response.ok && !upstreamFailure;
  const badge = $(action === "compile" ? "compileStatusBadge" : "publishStatusBadge");
  const caption = $(action === "compile" ? "compileResultCaption" : "publishResultCaption");
  badge.textContent = status || `HTTP ${response.status}`;
  badge.className = success ? "success-pill" : "error-pill";
  caption.textContent = success
    ? "操作已完成，详情见下方"
    : `请求未完成 · HTTP ${response.status} · 详细错误见下方`;
  panel.classList.toggle("outcome-error", !success);
  if (!success) {
    panel.querySelector("details").open = true;
    showNotice(`${action} HTTP ${response.status}：${errorText(response)}`, "error");
  } else {
    showNotice(`${action} 返回 HTTP ${response.status}；具体状态以响应为准。`, "success");
  }
  panel.scrollIntoView({behavior: "smooth", block: "start"});
}
$("compileBtn").addEventListener("click", () =>
  busy("compileBtn", async () => backendAction("compile"))
);
$("publishBtn").addEventListener("click", () => {
  try {
    if (!st.created?.operatorId) throw new Error("缺少 operatorId");
    chosenBackend();
    currentOptions(); // Check JSON before opening the confirmation dialog.
    text("publishConfirmOperator", st.created.operatorId);
    text("publishConfirmBackend", $("backend").value);
    text("publishModalHint", $("backend").value === "nifi_native"
      ? "NiFi Native 会生成扩展包；若响应要求部署，后续仍需单独安装。"
      : "Runner 发布会由 Publish Service 内部解析运行时环境。");
    $("publishDialog").showModal();
  } catch (error) {
    $("advancedOptions").open = true;
    showNotice(error.message || String(error), "error");
  }
});
$("cancelPublishBtn").addEventListener("click", () => {
  $("publishDialog").close();
});
$("confirmPublishBtn").addEventListener("click", () => {
  $("publishDialog").close();
  busy("publishBtn", async () => backendAction("publish"));
});
$("getOperatorBtn").addEventListener("click", () =>
  busy("getOperatorBtn", async () => {
    if (!st.created?.operatorId) throw new Error("缺少 operatorId");
    const response = await api(
      "GET", `/api/operators/${safePart(st.created.operatorId, "operatorId")}`
    );
    $("operatorReadResult").classList.remove("hidden");
    $("operatorReadJson").textContent = pretty(response.data);
    showNotice(
      response.ok
        ? "算子信息已更新。"
        : `Get Operator HTTP ${response.status}：${errorText(response)}`,
      response.ok ? "success" : "error"
    );
  })
);

/* Startup only introspects Publish Service. OpenAPI is requested separately
   so Analyze can still be tried even if schema introspection is disabled. */
async function startup() {
  try {
    const health = await api("GET", "/api/health");
    if (!health.ok) throw new Error(`HTTP ${health.status}：${errorText(health)}`);
    st.health = plain(health.data) ? health.data : null;
    text("serviceStatusText",
      `Publish Service · ${st.health?.version || "version unknown"}`);
    $("serviceStatus").className = "service-state ok";
  } catch (error) {
    st.health = null;
    text("serviceStatusText", "Publish Service · 未连接");
    $("serviceStatus").className = "service-state error";
    showNotice(
      `Publish /health 不可达：${error.message}。请检查 MPR_PUBLISH_SERVICE_URL。`,
      "error"
    );
  }
  await schemaOrWarning();
}
startup();
