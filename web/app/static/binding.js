"use strict";
/* Contract binding controls. JSON field keys and allowed values come from
   the running Publish Service schema and its Analyze response. */
(function installBindingEditor(root) {
  const S = root.SchemaTools;
  const Forms = root.FormBuilder;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value || {}, key);
  const isObject = (value) => !!value && typeof value === "object" && !Array.isArray(value);
  const compact = (key) => String(key).replace(/[_\s-]/g, "").toLowerCase();

  const SOURCES = {
    "input.payload": {
      title: "消息内容",
      detail: "使用传入消息的正文作为参数值。",
      glyph: "≡",
    },
    "input.metadata": {
      title: "消息属性",
      detail: "从传入消息的属性中读取一个值。",
      glyph: "⌗",
    },
    "operator.parameter": {
      title: "算子参数",
      detail: "读取配置算子时填写的参数。",
      glyph: "⚙",
    },
    constant: {
      title: "固定值",
      detail: "为此参数填写一个固定的值。",
      glyph: "=",
    },
  };

  const ROLE_PATTERNS = [
    [/^(?:metadata|attribute|attr)(?:key|name|path|value|id)?$/i, "input.metadata"],
    [/^(?:parameter|property)(?:key|name|path|value|id)?$/i, "operator.parameter"],
    [/^(?:constant|literal)(?:value|type|json)?$/i, "constant"],
    [/^(?:payload|content)(?:key|name|path|value|codec)?$/i, "input.payload"],
  ];

  function element(tag, cls, text) {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (text != null) result.textContent = String(text);
    return result;
  }

  function fieldRole(name, definition, doc) {
    // The actual schema is still the only source of field names. The role
    // classification below is a presentation heuristic, not a new API key.
    const schema = S.flatten(definition, doc);
    const semantic = typeof schema["x-binding-source"] === "string"
      ? schema["x-binding-source"] : null;
    if (semantic && SOURCES[semantic]) return semantic;
    const key = compact(name);
    if (key === "value") return "constant";
    for (const [pattern, source] of ROLE_PATTERNS) {
      if (pattern.test(key)) return source;
    }
    return null;
  }

  function fieldLabel(name, definition, doc, source) {
    const key = compact(name);
    const labels = {
      source: "数据来源",
      bindingsource: "数据来源",
      metadatakey: "属性名称",
      metadataname: "属性名称",
      attributekey: "属性名称",
      attributename: "属性名称",
      parametername: "参数名称",
      parameterkey: "参数名称",
      propertyname: "参数名称",
      propertykey: "参数名称",
      payloadpath: "内容路径",
      contentpath: "内容路径",
      literalvalue: "固定值",
      constantvalue: "固定值",
      codec: "编码格式",
      type: "数据类型",
      key: source === "input.metadata" ? "属性名称"
        : source === "operator.parameter" ? "参数名称" : "键名",
      value: source === "constant" ? "固定值" : "取值",
      path: "读取路径",
      defaultvalue: "默认值",
    };
    if (labels[key]) return labels[key];
    const schema = S.flatten(definition, doc);
    if (schema.description && schema.title && schema.title !== name &&
        !/^[A-Z][A-Za-z\s]+$/.test(schema.title)) return schema.title;
    const readable = String(name).replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/_/g, " ");
    return schema.title && schema.title !== name ? schema.title : readable;
  }

  function sourceKeyOf(schema, doc, allowed) {
    const flattened = S.flatten(schema, doc);
    let key = S.sourceField(schema, doc, allowed);
    if (key) return key;
    const choices = flattened.oneOf || flattened.anyOf || [];
    const candidates = new Set();
    for (const variant of choices) {
      const branch = S.flatten(variant, doc);
      const result = S.sourceField(branch, doc, allowed);
      if (result) candidates.add(result);
    }
    return candidates.size === 1 ? [...candidates][0] : null;
  }

  function sourceVariant(schema, doc, source, key) {
    const base = S.flatten(schema, doc);
    const choices = base.oneOf || base.anyOf || [];
    if (!choices.length) return base;
    for (const choice of choices) {
      const branch = S.flatten(choice, doc);
      const definition = branch.properties?.[key];
      if (!definition) continue;
      const possible = S.options(definition, doc);
      if (possible?.includes(source)) {
        const {oneOf, anyOf, ...parent} = base;
        return {
          ...parent,
          ...branch,
          type: "object",
          properties: {...(parent.properties || {}), ...(branch.properties || {})},
          required: [...new Set([...(parent.required || []), ...(branch.required || [])])],
        };
      }
    }
    return base;
  }

  function candidateSources(schema, doc, allowed, key) {
    const base = S.flatten(schema, doc);
    const declared = S.options(base.properties?.[key] || {}, doc);
    const variants = base.oneOf || base.anyOf || [];
    const variantDeclared = variants.flatMap((choice) => {
      const branch = S.flatten(choice, doc);
      return S.options(branch.properties?.[key] || {}, doc) || [];
    });
    const permitted = declared?.length ? declared
      : variantDeclared.length ? variantDeclared : null;
    return allowed.filter((source) => !permitted || permitted.includes(source));
  }

  function valueInput(schema, doc, initial) {
    // An unconstrained "value" from the real schema can hold multiple JSON
    // types. A type picker is a UI control only; it adds NO type field to API.
    const actual = S.flatten(schema, doc);
    const kind = S.schemaType(actual, doc);
    const unconstrained = kind === "unknown" ||
      (kind === "object" && !actual.properties && !actual.additionalProperties);
    if (!unconstrained) return Forms.editor(schema, {
      doc, name: "固定值", initial,
    });

    const host = element("div", "quick-constant");
    const label = element("label", "quick-type", "值的类型");
    const selector = element("select");
    for (const [value, caption] of [
      ["string", "文本"],
      ["number", "数字"],
      ["boolean", "是 / 否"],
      ["object", "对象或数组"],
      ["null", "空值"],
    ]) {
      const option = element("option", "", caption);
      option.value = value;
      selector.append(option);
    }
    label.append(selector);
    host.append(label);
    const panels = element("div");
    host.append(panels);
    const caches = new Map();

    function typeFor(value) {
      if (value === null) return "null";
      if (typeof value === "boolean") return "boolean";
      if (typeof value === "number") return "number";
      if (typeof value === "string") return "string";
      return "object";
    }
    let selected = initial === undefined ? "string" : typeFor(initial);
    function mount(kindName) {
      if (caches.has(kindName)) return caches.get(kindName);
      const area = element("div", "quick-constant-value");
      let input;
      if (kindName === "null") {
        area.append(element("p", "muted", "此参数使用空值。"));
        caches.set(kindName, {area, read: () => null});
        return caches.get(kindName);
      }
      if (kindName === "boolean") {
        input = element("select");
        [["true", "是"], ["false", "否"]].forEach(([value, caption]) => {
          const option = element("option", "", caption);
          option.value = value;
          input.append(option);
        });
        input.value = initial === true ? "true" : "false";
        area.append(input);
        caches.set(kindName, {
          area, read: () => input.value === "true",
        });
        return caches.get(kindName);
      }
      input = element(kindName === "object" ? "textarea" : "input",
        kindName === "object" ? "json-editor small" : "");
      if (kindName === "number") {
        input.type = "number";
        input.step = "any";
        input.placeholder = "例如：42";
      } else if (kindName === "object") {
        input.spellcheck = false;
        input.placeholder = '{"name": "example"} 或 [1, 2]';
      } else {
        input.type = "text";
        input.placeholder = "请输入固定文本";
      }
      input.value = initial !== undefined && typeFor(initial) === kindName
        ? (kindName === "object" ? JSON.stringify(initial, null, 2) : String(initial))
        : kindName === "object" ? "{}" : "";
      area.append(input);
      caches.set(kindName, {
        area,
        read() {
          if (kindName === "number") {
            if (!input.value.trim()) throw new Error("请填写一个数字");
            const n = Number(input.value);
            if (!Number.isFinite(n)) throw new Error("数字无效");
            return n;
          }
          if (kindName === "object") {
            let data;
            try { data = JSON.parse(input.value); }
            catch { throw new Error("对象或数组的 JSON 格式不正确"); }
            if (data === null || typeof data !== "object") {
              throw new Error("请选择对象或数组，其他类型可在左侧切换");
            }
            return data;
          }
          return input.value;
        },
      });
      return caches.get(kindName);
    }

    function choose(name) {
      selected = name;
      const chosen = mount(name);
      panels.replaceChildren(chosen.area);
    }
    selector.value = selected;
    choose(selected);
    selector.addEventListener("change", () => choose(selector.value));
    return {
      element: host,
      read() { return caches.get(selected).read(); },
    };
  }

  function create({schema, doc, allowedSources, name, initial, onChange}) {
    const allowed = Array.isArray(allowedSources) ? [...new Set(allowedSources)] : [];
    const sourceKey = sourceKeyOf(schema, doc, allowed);
    const rootUI = element("div", "binding-choice-editor");
    const layout = element("div", "source-choice-grid");
    rootUI.append(element("div", "binding-source-label", "数据来源"));
    rootUI.append(layout);
    const note = element("div", "source-current-hint");
    note.setAttribute("aria-live", "polite");
    rootUI.append(note);
    const fieldHost = element("div", "source-fields");
    rootUI.append(fieldHost);
    const selectedInputs = [];
    const panels = new Map();
    const sources = sourceKey ? candidateSources(schema, doc, allowed, sourceKey) : [];

    if (!sourceKey || !sources.length) {
      rootUI.replaceChildren();
      rootUI.append(element("p", "muted",
        "当前参数的绑定方式需要填写完整配置。"));
      const fallback = Forms.editor(schema, {
        doc, name, initial, allowedSources: allowed,
      });
      rootUI.append(fallback.element);
      return {
        element: rootUI,
        read() { return fallback.read(); },
        apply(value) { return false; },
      };
    }

    const shared = S.flatten(schema, doc);
    const hasSourceVariants = !!(shared.oneOf?.length || shared.anyOf?.length);
    const allProperties = shared.properties || {};
    let initialData = initial;
    const defaultSource = initial && sources.includes(initial[sourceKey])
      ? initial[sourceKey] : null;
    let selectedSource = null;
    const radioName = `source-${Math.random().toString(36).slice(2)}`;

    function makePanel(source) {
      if (panels.has(source)) return panels.get(source);
      const resolved = sourceVariant(schema, doc, source, sourceKey);
      const properties = resolved.properties || allProperties;
      const required = new Set(resolved.required || []);
      const concrete = Object.entries(properties).filter(([key, item]) =>
        key !== sourceKey && !S.flatten(item, doc).readOnly
      );
      const explicitBranch = hasSourceVariants &&
        (resolved.oneOf === undefined && resolved.anyOf === undefined);
      const mainKeys = [];
      const secondaryKeys = [];

      for (const [key, definition] of concrete) {
        if (explicitBranch) {
          if (required.has(key) || fieldRole(key, definition, doc) === source) {
            mainKeys.push(key);
          } else {
            secondaryKeys.push(key);
          }
          continue;
        }
        const role = fieldRole(key, definition, doc);
        if (role && role !== source && !required.has(key)) continue;
        const keyField = compact(key) === "key" &&
          (source === "input.metadata" || source === "operator.parameter");
        const contentPath = compact(key) === "path" && source === "input.payload";
        if (role === source || required.has(key) || keyField || contentPath) {
          mainKeys.push(key);
        } else {
          secondaryKeys.push(key);
        }
      }

      const panel = element("div", "source-config");
      const primary = element("div", "source-main-fields");
      panel.append(primary);
      const inputs = [];

      function appendField(key, target, advanced) {
        const definition = properties[key];
        const shape = S.flatten(definition, doc);
        const wrapper = element("div", "source-property source-form-field");
        const title = element("label", "source-property-label source-field-label",
          fieldLabel(key, definition, doc, source));
        const requiredByApi = required.has(key);
        const sourceSpecific = fieldRole(key, definition, doc) === source;
        if (requiredByApi) {
          title.append(element("span", "source-required", "必填"));
        } else if (advanced) {
          title.append(element("span", "source-optional", "选填"));
        }
        wrapper.append(title);
        if (shape.description) {
          wrapper.append(element("p", "source-field-hint", shape.description));
        }
        const given = initialData && initialData[sourceKey] === source &&
          own(initialData, key);
        const preset = given ? initialData[key] :
          own(shape, "default") ? shape.default : undefined;
        const item = key === "value" && source === "constant"
          ? valueInput(definition, doc, preset)
          : Forms.editor(definition, {
              doc, name: `${name}.${key}`, initial: preset,
            });
        wrapper.append(item.element);
        const record = {
          key, item,
          required: requiredByApi,
          isConstantValue: source === "constant" && key === "value",
          keyRequired: /^(?:metadata|attribute|attr|parameter|property)(?:key|name)$/i.test(compact(key))
            && fieldRole(key, definition, doc) === source,
          interacted: given,
        };
        wrapper.addEventListener("input", () => {
          record.interacted = true;
          onChange?.();
        });
        wrapper.addEventListener("change", () => {
          record.interacted = true;
          onChange?.();
        });
        inputs.push(record);
        target.append(wrapper);
      }

      mainKeys.forEach((key) => appendField(key, primary, false));
      if (!mainKeys.length) {
        primary.append(element("p", "source-empty",
          "此来源无需额外填写。"));
      }
      if (secondaryKeys.length) {
        const details = element("details", "source-advanced");
        details.append(element("summary", "", "更多设置"));
        const fields = element("div", "source-extra-fields");
        secondaryKeys.forEach((key) => appendField(key, fields, true));
        details.append(fields);
        panel.append(details);
      }

      const record = {
        panel,
        read() {
          const result = {[sourceKey]: source};
          for (const field of inputs) {
            if (field.keyRequired && !field.interacted) {
              throw new Error(`请填写「${fieldLabel(field.key, properties[field.key], doc, source)}」`);
            }
            if (!field.required && !field.isConstantValue && !field.interacted) {
              continue;
            }
            const value = field.item.read();
            if (field.keyRequired && typeof value === "string" && !value.trim()) {
              throw new Error(`请填写「${fieldLabel(field.key, properties[field.key], doc, source)}」`);
            }
            result[field.key] = value;
          }
          return result;
        },
      };
      panels.set(source, record);
      return record;
    }

    function change(source) {
      selectedSource = source;
      const definition = SOURCES[source];
      note.textContent = definition?.detail || "使用所选的数据来源。";
      const panel = makePanel(source);
      fieldHost.replaceChildren(panel.panel);
      layout.querySelectorAll("label").forEach((item) => {
        item.classList.toggle("chosen", item.dataset.value === source);
        item.querySelector("input").checked = item.dataset.value === source;
      });
      onChange?.();
    }

    for (const source of sources) {
      const detail = SOURCES[source] || {
        title: source,
        detail: "使用此来源提供参数值。",
        glyph: "•",
      };
      const tile = element("label", "source-choice");
      tile.dataset.value = source;
      const radio = element("input");
      radio.type = "radio";
      radio.name = radioName;
      radio.value = source; // The exact machine value is never translated.
      tile.append(radio);
      const symbol = element("span", "source-icon", detail.glyph);
      symbol.setAttribute("aria-hidden", "true");
      tile.append(symbol);
      const description = element("span", "source-choice-content");
      description.append(element("strong", "", detail.title));
      description.append(element("small", "", detail.detail));
      tile.append(description);
      radio.addEventListener("change", () => change(source));
      layout.append(tile);
      selectedInputs.push(radio);
    }

    if (defaultSource) change(defaultSource);
    else {
      fieldHost.append(element("div", "source-empty",
        "选择上方的数据来源后，填写对应的信息。"));
      note.textContent = "";
    }

    return {
      element: rootUI,
      read() {
        if (!selectedSource) throw new Error(`请先为「${name}」选择数据来源`);
        return makePanel(selectedSource).read();
      },
      apply(values) {
        if (!isObject(values) || !sources.includes(values[sourceKey])) {
          return false;
        }
        // Recreate only this source's fields, keeping other source drafts.
        initialData = {...values};
        panels.delete(values[sourceKey]);
        change(values[sourceKey]);
        return true;
      },
      get source() { return selectedSource; },
      getSource() { return selectedSource; },
    };
  }

  const api = {
    create, sources: SOURCES, fieldLabel, fieldRole,
    displaySource: (source) => SOURCES[source] || {title: source, detail: ""},
    makeEditor({schema, doc, parameterName, allowedSources, initial, onUpdate}) {
      return create({
        schema, doc, name: parameterName,
        allowedSources, initial, onChange: onUpdate,
      });
    },
    fromSuggestion(suggestion, schema, doc, allowedSources) {
      return S.fromSuggestion(suggestion, schema, doc, allowedSources);
    },
  };
  root.BindingEditor = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
