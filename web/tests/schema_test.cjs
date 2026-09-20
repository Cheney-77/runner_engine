"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const S = require(path.resolve(__dirname, "../app/static/schema.js"));

// Schema fixtures represent known root fields only; nested structures here
// are explicitly illustrative mock models, not assertions about user code.
const doc = {
  paths: {
    "/v1/operators": {
      post: {
        requestBody: {
          content: {"application/json": {
            schema: {$ref: "#/components/schemas/CreateVirtualContractRequest"},
          }},
        },
      },
    },
  },
  components: {
    schemas: {
      CreateVirtualContractRequest: {
        type: "object",
        required: ["workspace", "sourceRevision", "pythonRoot", "operator", "callableId"],
        properties: {
          workspace: {type: "string"},
          sourceRevision: {type: "string"},
          pythonRoot: {type: "string"},
          callableId: {type: "string"},
          operator: {$ref: "#/components/schemas/OperatorSelection"},
          argumentBindings: {
            type: "object",
            additionalProperties: {$ref: "#/components/schemas/BindingSelection"},
          },
          constructorBindings: {
            type: "object",
            additionalProperties: {$ref: "#/components/schemas/BindingSelection"},
          },
          output: {$ref: "#/components/schemas/OutputSelection"},
        },
      },
      OperatorSelection: {
        type: "object", required: ["name"],
        properties: {
          name: {type: "string"},
          description: {type: ["string", "null"], default: null},
        },
      },
      BindingSelection: {
        type: "object", required: ["bindingKind"],
        properties: {
          bindingKind: {
            enum: ["input.payload", "input.metadata", "operator.parameter", "constant"],
          },
          contentPath: {type: "string"},
        },
      },
      OutputSelection: {type: "object", properties: {
        target: {enum: ["content", "metadata"]},
      }},
    },
  },
};

const root = S.requestSchema(doc, "/v1/operators");
assert.equal(S.propertyName(root, doc, "source_revision"), "sourceRevision");
assert.equal(S.propertyName(root, doc, "python_root"), "pythonRoot");
assert.equal(S.propertyName(root, doc, "callable_id"), "callableId");
assert.equal(S.propertyName(root, doc, "argument_bindings"), "argumentBindings");
assert.equal(S.propertyName(root, doc, "bad_future_field"), null);

const binding = S.dictionaryValueSchema(
  S.propertySchema(root, doc, "argument_bindings"), doc
);
assert.equal(S.sourceField(binding, doc, ["input.payload", "constant"]), "bindingKind");

const recommendation = S.fromSuggestion(
  {binding: {bindingKind: "input.payload", contentPath: "$.text"}, reason: "mock"},
  binding, doc, ["input.payload", "constant"]
);
assert.deepEqual(recommendation, {
  bindingKind: "input.payload", contentPath: "$.text",
});
assert.equal(
  S.fromSuggestion({bindingKind: "operator.parameter"}, binding, doc, ["constant"]),
  null, "unallowed Analyze source must never be applied"
);

const sample = S.template(S.propertySchema(root, doc, "operator"), doc);
assert.deepEqual(sample, {name: ""});
const output = S.template(S.propertySchema(root, doc, "output"), doc);
assert.deepEqual(output, {});
assert.equal(S.template({enum:["bytes","json"]},doc), undefined,
  "enum ordering must never set the default codec");
assert.equal(S.template({enum:["bytes","json"],default:"json"},doc), "json",
  "an explicit server-side default is respected");

console.log("PASS: camelCase aliases, Schema refs, source filtering, nested model templates");
