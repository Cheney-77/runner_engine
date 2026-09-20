"use strict";
const assert=require("node:assert/strict");
const path=require("node:path");
require(path.resolve(__dirname,"../app/static/schema.js"));
const B=require(path.resolve(__dirname,"../app/static/binding.js"));

const flat={
  type:"object",
  required:["bindingSource"],
  properties:{
    bindingSource:{enum:["input.payload","input.metadata","operator.parameter","constant"]},
    payloadPath:{type:"string"},
    metadataKey:{type:"string"},
    parameterName:{type:"string"},
    value:{},
    codec:{enum:["bytes","json"]},
  },
};
const allowed=["input.payload","input.metadata","operator.parameter","constant"];
const choice=B.sourceChoices(flat,{},allowed);
assert.equal(choice.mode,"flat");
assert.equal(choice.sourceKey,"bindingSource");
assert.deepEqual(choice.allowed,allowed);
const meta=B.filterSchema(flat,{},"input.metadata","bindingSource");
assert.ok(meta.main.metadataKey);
assert.ok(!meta.main.payloadPath);
assert.ok(!meta.main.parameterName);
assert.ok(!meta.main.value);
assert.ok(!meta.main.codec && !meta.extra.codec);
assert.ok(B.filterSchema(flat,{},"input.payload","bindingSource").main.codec);
const constant=B.filterSchema(flat,{},"constant","bindingSource");
assert.ok(constant.main.value);
assert.ok(!constant.main.metadataKey);
assert.ok(!constant.main.parameterName);
assert.equal(B.displaySource("input.metadata").title,"输入属性");
assert.equal(B.displaySource("constant").title,"固定值");

const unionDoc={
  components:{schemas:{
    Payload:{type:"object",required:["source"],properties:{
      source:{const:"input.payload"},payloadPath:{type:"string"},
      description:{type:"string"},
    }},
    Constant:{type:"object",required:["source"],properties:{
      source:{const:"constant"},value:{},
      description:{type:"string"},
    }},
  }},
};
const union={
  oneOf:[
    {$ref:"#/components/schemas/Payload"},
    {$ref:"#/components/schemas/Constant"},
  ],
  discriminator:{propertyName:"source"},
};
const modes=B.sourceChoices(union,unionDoc,["input.payload","constant"]);
assert.equal(modes.mode,"union");
assert.equal(modes.branches.size,2);
assert.ok(B.filterSchema(modes.branches.get("constant"),unionDoc,"constant","source").main.value);
assert.ok(!B.filterSchema(modes.branches.get("constant"),unionDoc,"constant","source").main.payloadPath);
assert.equal(
  B.fromSuggestion({description:"both branches"},union,unionDoc,["input.payload","constant"]),
  null,
  "an ambiguous suggestion must not select an arbitrary source"
);
assert.deepEqual(
  B.fromSuggestion({source:"constant",value:99},union,unionDoc,["input.payload","constant"]),
  {source:"constant",value:99}
);
const unsupported=B.sourceChoices({type:"object",properties:{value:{}}},{},allowed);
assert.equal(unsupported.mode,"unsupported");
console.log("PASS: friendly source labels, source-specific fields, aliases, discriminated unions, suggestion safety");
