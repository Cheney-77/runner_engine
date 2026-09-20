"use strict";
/*
 * OpenAPI helpers are intentionally independent of the deployment's
 * CreateVirtualContractRequest nested model definitions. They inspect the
 * *running* Publish Service schema instead of inventing Python model fields.
 */
(function installSchemaTools(root) {
  const has = (o, key) => Object.prototype.hasOwnProperty.call(o || {}, key);
  const normalize = (name) => String(name).replace(/[_\-\s]/g, "").toLowerCase();

  function lookupRef(doc, ref) {
    if (typeof ref !== "string" || !ref.startsWith("#/")) return null;
    let item = doc;
    for (const encodedPart of ref.slice(2).split("/")) {
      const part = encodedPart.replace(/~1/g, "/").replace(/~0/g, "~");
      item = item?.[part];
      if (item === undefined) return null;
    }
    return item;
  }

  function deref(schema, doc, visited = new Set()) {
    if (!schema || typeof schema !== "object") return {};
    if (!schema.$ref) return schema;
    if (visited.has(schema.$ref)) return {};
    const resolved = lookupRef(doc, schema.$ref);
    if (!resolved) return {};
    const target = deref(resolved, doc, new Set([...visited, schema.$ref]));
    const { $ref, ...rest } = schema;
    return { ...target, ...rest };
  }

  function flatten(schema, doc, depth = 0) {
    if (depth > 12) return {};
    const current = deref(schema, doc);
    if (!Array.isArray(current.allOf)) return current;
    const base = { ...current };
    delete base.allOf;
    for (const element of current.allOf) {
      const branch = flatten(element, doc, depth + 1);
      base.properties = { ...(base.properties || {}), ...(branch.properties || {}) };
      base.required = [
        ...new Set([...(base.required || []), ...(branch.required || [])]),
      ];
      if (!base.type && branch.type) base.type = branch.type;
      if (!base.additionalProperties && branch.additionalProperties) {
        base.additionalProperties = branch.additionalProperties;
      }
    }
    return base;
  }

  function options(schema, doc) {
    const s = flatten(schema, doc);
    if (has(s, "const")) return [s.const];
    if (Array.isArray(s.enum)) return s.enum;
    if (Array.isArray(s.anyOf) || Array.isArray(s.oneOf)) {
      const variants = s.anyOf || s.oneOf;
      const results = variants.flatMap((v) => options(v, doc) || []);
      return results.length ? [...new Set(results)] : null;
    }
    return null;
  }

  function schemaType(schema, doc) {
    const s = flatten(schema, doc);
    if (Array.isArray(s.type)) return s.type.find((item) => item !== "null") || "null";
    if (s.type) return s.type;
    if (s.properties || s.additionalProperties) return "object";
    if (s.items) return "array";
    if (s.anyOf?.length || s.oneOf?.length) {
      const nonNull = (s.anyOf || s.oneOf).find(
        (option) => schemaType(option, doc) !== "null"
      );
      return nonNull ? schemaType(nonNull, doc) : "null";
    }
    const variants = options(s, doc);
    if (variants?.length) return typeof variants[0];
    return "unknown";
  }

  function requestSchema(doc, path, method = "post") {
    const operation = doc?.paths?.[path]?.[method];
    if (!operation) return null;
    const requestBody = flatten(operation.requestBody, doc);
    const content = requestBody.content || {};
    const media = content["application/json"] ||
      Object.entries(content).find(([name]) => name.endsWith("+json"))?.[1];
    return media?.schema || null;
  }

  function propertyName(schema, doc, pythonName) {
    const props = flatten(schema, doc).properties || {};
    if (has(props, pythonName)) return pythonName;
    const normalized = normalize(pythonName);
    return Object.keys(props).find((key) => normalize(key) === normalized) || null;
  }

  function propertySchema(schema, doc, pythonName) {
    const key = propertyName(schema, doc, pythonName);
    return key ? flatten(schema, doc).properties[key] : null;
  }

  function dictionaryValueSchema(schema, doc) {
    const item = flatten(schema, doc);
    return typeof item.additionalProperties === "object" ?
      item.additionalProperties : null;
  }

  function template(schema, doc, depth = 0, visited = new Set()) {
    if (!schema || depth > 12) return null;
    if (schema.$ref) {
      if (visited.has(schema.$ref)) return null;
      return template(
        lookupRef(doc, schema.$ref), doc, depth + 1,
        new Set([...visited, schema.$ref])
      );
    }
    const s = flatten(schema, doc);
    if (has(s, "default")) return s.default;
    if (has(s, "example")) return s.example;
    if (has(s, "const")) return s.const;
    // Enum ordering is not an application default. Require an explicit
    // user choice unless Pydantic's schema declares default or const.
    if (s.enum?.length) return undefined;
    if (s.anyOf?.length || s.oneOf?.length) {
      const variants = s.anyOf || s.oneOf;
      const first = variants.find((choice) => schemaType(choice, doc) !== "null");
      return template(first || variants[0], doc, depth + 1, visited);
    }
    if (s.type === "null") return null;
    if (s.type === "object" || s.properties || s.additionalProperties) {
      const result = {};
      const required = new Set(s.required || []);
      for (const [key, child] of Object.entries(s.properties || {})) {
        const field = flatten(child, doc);
        if (field.readOnly || !required.has(key)) continue;
        result[key] = template(child, doc, depth + 1, visited);
      }
      return result;
    }
    if (s.type === "array") return [];
    if (s.type === "integer" || s.type === "number") return 0;
    if (s.type === "boolean") return false;
    return "";
  }

  function sourceField(schema, doc, allowedSources) {
    const props = flatten(schema, doc).properties || {};
    if (has(props, "source")) return "source";
    const plausible = Object.keys(props).filter((name) =>
      normalize(name).includes("source")
    );
    if (plausible.length === 1) return plausible[0];
    const matching = Object.entries(props).filter(([, field]) => {
      const enums = options(field, doc);
      return enums && enums.some((v) => allowedSources.includes(v));
    });
    return matching.length === 1 ? matching[0][0] : null;
  }

  function fromSuggestion(suggestion, schema, doc, allowedSources) {
    if (!suggestion || typeof suggestion !== "object" ||
        Array.isArray(suggestion)) return null;
    const props = flatten(schema, doc).properties || {};
    const possibilities = [suggestion];
    for (const value of Object.values(suggestion)) {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        possibilities.push(value);
      }
    }
    const sourceKey = sourceField(schema, doc, allowedSources);
    for (const item of possibilities) {
      const recognized = Object.keys(item).filter((key) => has(props, key));
      if (!recognized.length) continue;
      const values = Object.fromEntries(recognized.map((key) => [key, item[key]]));
      const source = sourceKey ? values[sourceKey] : undefined;
      if (source !== undefined && !allowedSources.includes(source)) continue;
      return values;
    }
    return null;
  }

  const api = {
    has, lookupRef, deref, flatten, options, schemaType, requestSchema,
    propertyName, propertySchema, dictionaryValueSchema, template,
    sourceField, fromSuggestion,
  };
  root.SchemaTools = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
