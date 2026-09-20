"use strict";
/* Runtime OpenAPI-driven field editor. No handwritten nested Pydantic fields. */
(function installForm(root) {
  const S = root.SchemaTools;

  function node(tag, css, text) {
    const item = document.createElement(tag);
    if (css) item.className = css;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }
  function plain(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }
  function json(value) {
    return JSON.stringify(value, null, 2);
  }
  function fail(name, message) {
    throw new Error(`${name}: ${message}`);
  }

  function keyName(name, schema) {
    const value = String(name).split(".").at(-1);
    const key = value.replace(/[_\s-]/g, "").toLowerCase();
    const dictionary = {
      source: "数据来源",
      bindingsource: "数据来源",
      codec: "编码格式",
      payload: "消息正文",
      output: "输出结果",
      name: "名称",
      displayname: "显示名称",
      description: "说明",
      format: "格式",
      path: "路径",
      value: "取值",
      key: "键名",
      metadata: "消息属性",
      attributes: "消息属性",
      parameters: "算子参数",
      parametervalue: "参数值",
      payloadpath: "内容路径",
      metadatakey: "属性名称",
      parametername: "参数名称",
    };
    if (dictionary[key]) return dictionary[key];
    if (schema.description && schema.title && schema.title !== value) {
      return schema.title;
    }
    return value.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ");
  }

  function choiceTitle(value) {
    const titles = {
      bytes: "字节 / 文本（bytes）",
      json: "JSON（对象、列表等）",
      utf8: "UTF-8 文本",
      text: "文本",
      "input.payload": "消息内容",
      "input.metadata": "消息属性",
      "operator.parameter": "算子参数",
      constant: "固定值",
      true: "是",
      false: "否",
    };
    return typeof value === "string" && titles[value]
      ? titles[value] : String(value);
  }

  function rawEditor(schema, config) {
    const s = S.flatten(schema, config.doc);
    const root = node("div", "schema-group");
    const area = node("textarea", "json-editor small");
    area.spellcheck = false;
    const initial = config.initial !== undefined
      ? config.initial : S.template(schema, config.doc);
    area.value = json(initial === undefined ? null : initial);
    root.append(area);
    const kind = S.schemaType(s, config.doc);
    return {
      element: root,
      read() {
        let data;
        try { data = JSON.parse(area.value); }
        catch (error) { fail(config.name || "JSON", `JSON 无法解析：${error.message}`); }
        if (kind === "object" && !plain(data)) fail(config.name, "必须是 JSON object");
        if (kind === "array" && !Array.isArray(data)) fail(config.name, "必须是 JSON array");
        return data;
      },
    };
  }

  function editor(schema, config = {}) {
    const doc = config.doc;
    const title = config.name || "字段";
    const depth = config.depth || 0;
    if (depth > 10) return rawEditor(schema, config);

    const s = S.flatten(schema, doc);
    const choices = s.oneOf || s.anyOf;
    if (choices?.length) {
      const variants = choices.map((part) => ({
        schema: part,
        shape: S.flatten(part, doc),
      }));
      // A T | null is edited as T; optionality is handled at object field level.
      const real = variants.filter((choice) => S.schemaType(choice.schema, doc) !== "null");
      const nullable = variants.length !== real.length;
      if (real.length === 1 && nullable) {
        return editor(real[0].schema, {...config, depth: depth + 1});
      }
      if (real.length === 1 && !nullable) {
        return editor(real[0].schema, {...config, depth: depth + 1});
      }
      const block = node("div", "schema-group");
      const select = node("select");
      variants.forEach((item, i) => {
        const option = node("option", "", item.shape.title ||
          (S.schemaType(item.schema, doc) === "null" ? "null" : `选项 ${i + 1}`));
        option.value = String(i);
        select.append(option);
      });
      block.append(select);
      const holder = node("div");
      block.append(holder);
      let chosen = null;
      function refresh() {
        holder.replaceChildren();
        const which = variants[Number(select.value)];
        if (S.schemaType(which.schema, doc) === "null") {
          chosen = {read: () => null};
          return;
        }
        chosen = editor(which.schema, {
          ...config,
          depth: depth + 1,
          initial: config.initial,
        });
        holder.append(chosen.element);
      }
      select.value = String(
        variants.findIndex((choice) =>
          config.initial === null && S.schemaType(choice.schema, doc) === "null"
        )
      );
      if (select.value === "-1") select.value = "0";
      select.addEventListener("change", refresh);
      refresh();
      return {element: block, read: () => chosen.read()};
    }

    const allowed = config.allowedSources;
    const enumValues = S.options(s, doc);
    const kind = S.schemaType(s, doc);
    // allowedSources describes the BindingSelection SOURCE PROPERTY,
    // not the whole BindingSelection object.
    if ((enumValues && enumValues.length) ||
        (allowed && kind === "string")) {
      const block = node("div", "schema-group");
      const select = node("select");
      const values = enumValues && allowed
        ? enumValues.filter((option) => allowed.includes(option))
        : (allowed || enumValues);
      if (!values?.length) {
        block.append(node("p", "editor-error",  "当前参数没有可用的配置选项。"));
        return {
          element: block,
          read: () => fail(title,  "没有可用的配置选项"),
        };
      }
      const placeholder = node("option", "", "请选择");
      placeholder.value = "";
      select.append(placeholder);
      for (const value of values) {
        const option = node("option", "", choiceTitle(value));
        option.value = JSON.stringify(value);
        const friendly = {
          bytes: "原始字节（bytes）",
          json: "JSON（对象或数组）",
          utf8: "UTF-8 文本",
          text: "文本",
          string: "字符串",
          null: "空值",
        };
        if (typeof value === "string" && Object.prototype.hasOwnProperty.call(friendly, value)) {
          option.textContent = friendly[value];
        }
        select.append(option);
      }
      if (config.initial !== undefined && values.some(
        (value) => JSON.stringify(value) === JSON.stringify(config.initial)
      )) {
        select.value = JSON.stringify(config.initial);
      } else {
        select.value = "";
      }
      block.append(select);
      return {
        element: block,
        read() {
          if (!select.value) fail(title, "请选择一个选项");
          return JSON.parse(select.value);
        },
      };
    }

    if (kind === "object") {
      const props = s.properties || {};
      if (!Object.keys(props).length) return rawEditor(schema, config);
      const holder = node("div", "schema-group schema-fields");
      const init = plain(config.initial) ? config.initial : {};
      const required = new Set(s.required || []);
      const sourceKey = allowed ? S.sourceField(schema, doc, allowed) : null;
      const fields = new Map();
      for (const [key, definition] of Object.entries(props)) {
        const fieldSchema = S.flatten(definition, doc);
        if (fieldSchema.readOnly) continue;
        const label = keyName(key, fieldSchema);
        const active = required.has(key) ||
          Object.prototype.hasOwnProperty.call(init, key);
        const wrapper = node("div", "schema-field");
        const heading = node("div", "schema-label");
        heading.append(node("span", "", label));
        if (required.has(key)) heading.append(node("span", "field-sub", "必填"));
        if (label !== key) heading.append(node("span", "field-sub", key));
        wrapper.append(heading);
        if (fieldSchema.description) {
          wrapper.append(node("p", "schema-description", fieldSchema.description));
        }
        let optional = null;
        if (!required.has(key)) {
          const row = node("label", "inline-check");
          optional = node("input");
          optional.type = "checkbox";
          optional.checked = active;
          row.append(optional);
          row.append(node("span", "", "使用此项"));
          wrapper.append(row);
        }
        // Do not silently select the first enum (especially output.codec
        // "bytes"). Values come from the user or an explicit schema default.
        const value = Object.prototype.hasOwnProperty.call(init, key)
          ? init[key]
          : Object.prototype.hasOwnProperty.call(fieldSchema, "default")
            ? fieldSchema.default : undefined;
        const child = editor(definition, {
          doc, name: `${title}.${key}`, depth: depth + 1, initial: value,
          allowedSources: key === sourceKey ? allowed : undefined,
        });
        const editorHolder = node("div",
          S.schemaType(definition, doc) === "object" ? "schema-field nested" : "");
        editorHolder.append(child.element);
        wrapper.append(editorHolder);
        if (optional) {
          editorHolder.classList.toggle("hidden", !optional.checked);
          optional.addEventListener("change", () => {
            editorHolder.classList.toggle("hidden", !optional.checked);
          });
        }
        holder.append(wrapper);
        fields.set(key, {child, optional, required: required.has(key)});
      }

      let extensions = null;
      if (s.additionalProperties) {
        const extras = node("div", "schema-field");
        extras.append(node("div", "schema-label", "其他配置（JSON）"));
        const area = node("textarea", "json-editor small");
        area.value = json(Object.fromEntries(
          Object.entries(init).filter(([key]) => !Object.prototype.hasOwnProperty.call(props, key))
        ));
        extras.append(area);
        holder.append(extras);
        extensions = area;
      }

      return {
        element: holder,
        read() {
          const result = {};
          for (const [key, info] of fields.entries()) {
            if (info.required || !info.optional || info.optional.checked) {
              result[key] = info.child.read();
            }
          }
          if (extensions) {
            let values;
            try { values = JSON.parse(extensions.value); }
            catch (error) { fail(title, `额外属性 JSON 无法解析：${error.message}`); }
            if (!plain(values)) fail(title, "额外属性必须是 object");
            for (const [key, value] of Object.entries(values)) {
              if (Object.prototype.hasOwnProperty.call(props, key)) {
                fail(title, `额外属性与已定义字段重复：${key}`);
              }
              result[key] = value;
            }
          }
          return result;
        },
      };
    }

    if (kind === "array" && s.items) {
      const container = node("div", "schema-group");
      const rows = node("div");
      container.append(rows);
      const controls = [];
      function add(value) {
        const row = node("div", "schema-field nested");
        const child = editor(s.items, {
          doc, name: `${title}[${controls.length}]`,
          depth: depth + 1,
          initial: value,
        });
        row.append(child.element);
        const remove = node("button", "button secondary small", "删除此项");
        remove.type = "button";
        row.append(remove);
        const record = {row, child};
        controls.push(record);
        remove.addEventListener("click", () => {
          row.remove();
          controls.splice(controls.indexOf(record), 1);
        });
        rows.append(row);
      }
      if (Array.isArray(config.initial)) config.initial.forEach(add);
      const button = node("button", "button secondary small", "添加一项");
      button.type = "button";
      button.addEventListener("click", () => add(S.template(s.items, doc)));
      container.append(button);
      return {
        element: container,
        read() {
          const values = controls.map(({child}) => child.read());
          if (s.minItems && values.length < s.minItems) {
            fail(title, `至少需要 ${s.minItems} 项`);
          }
          return values;
        },
      };
    }

    if (kind === "boolean") {
      const holder = node("div", "schema-group");
      const select = node("select");
      for (const [text, value] of [["是", "true"], ["否", "false"]]) {
        const option = node("option", "", text);
        option.value = value;
        select.append(option);
      }
      select.value = config.initial === true ? "true" : "false";
      holder.append(select);
      return {element: holder, read: () => select.value === "true"};
    }

    if (kind === "integer" || kind === "number") {
      const holder = node("div", "schema-group");
      const input = node("input");
      input.type = "number";
      input.step = kind === "integer" ? "1" : "any";
      if (s.minimum !== undefined) input.min = String(s.minimum);
      if (s.maximum !== undefined) input.max = String(s.maximum);
      input.value = config.initial === undefined || config.initial === null
        ? "" : String(config.initial);
      holder.append(input);
      return {
        element: holder,
        read() {
          if (input.value.trim() === "") fail(title, "请输入数字");
          const result = Number(input.value);
          if (!Number.isFinite(result) || (kind === "integer" && !Number.isInteger(result))) {
            fail(title, "数字类型不正确");
          }
          if (s.minimum !== undefined && result < s.minimum) fail(title, "低于最小值");
          if (s.maximum !== undefined && result > s.maximum) fail(title, "高于最大值");
          return result;
        },
      };
    }

    if (kind === "string") {
      const holder = node("div", "schema-group");
      const multiline = s.maxLength > 500 || (s.description || "").length > 300;
      const input = node(multiline ? "textarea" : "input");
      if (!multiline) input.type = "text";
      input.value = config.initial == null ? "" : String(config.initial);
      holder.append(input);
      return {
        element: holder,
        read() {
          if (s.minLength && input.value.length < s.minLength) {
            fail(title, `最少 ${s.minLength} 个字符`);
          }
          if (s.maxLength && input.value.length > s.maxLength) {
            fail(title, `最多 ${s.maxLength} 个字符`);
          }
          return input.value;
        },
      };
    }

    if (kind === "null") {
      const block = node("div", "muted", "null");
      return {element: block, read: () => null};
    }
    // 'Any', unconstrained dict/list, or an unfamiliar future model:
    // permit structured JSON without inventing a field type.
    return rawEditor(schema, config);
  }

  const api = {editor};
  root.FormBuilder = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
