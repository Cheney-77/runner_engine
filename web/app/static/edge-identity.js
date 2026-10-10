"use strict";

/* Device identity is a publish scope, not a device registry. Both fields form the identity. */
(() => {
  const object = (value) => value && typeof value === "object" && !Array.isArray(value);
  const text = (value) => typeof value === "string" && value.trim() ? value.trim() : null;

  function identityOf(record) {
    const options = object(record?.options) ? record.options : {};
    const manifest = object(record?.manifest) ? record.manifest : {};
    const candidate = [record?.edgeIdentity, record?.edge_identity, options.edgeIdentity,
      options.edge_identity, manifest.edgeIdentity, manifest.edge_identity]
      .find((value) => object(value)) || {};
    const tokenPair = text(candidate.tokenPair ?? candidate.token_pair ?? record?.token_pair);
    const edgeName = text(candidate.edgeName ?? candidate.edge_name ?? record?.edge_name);
    return {tokenPair, edgeName, legacy: !tokenPair && !edgeName, incomplete: !!(tokenPair && !edgeName || !tokenPair && edgeName)};
  }

  function targetOf(record) {
    return record?.targetPlatform || record?.target_platform || record?.options?.targetPlatform ||
      record?.options?.target_platform || record?.manifest?.targetPlatform || {};
  }

  function targetLabel(target) {
    if (!target) return "—";
    const platform = `${target.os || "—"} · ${target.arch || "—"}`;
    return target.pythonVersion || target.python_version
      ? `${platform} · Python ${target.pythonVersion || target.python_version}` : platform;
  }

  function fieldLabel(value, identity) {
    return value || (identity.legacy ? "历史未指定" : "未提供");
  }

  function scopeKey(record, userId) {
    const target = targetOf(record);
    const identity = identityOf(record);
    return JSON.stringify([
      String(record?.userId ?? userId ?? ""), target.os ?? null, target.arch ?? null,
      target.pythonVersion ?? target.python_version ?? null, identity.tokenPair, identity.edgeName,
    ]);
  }

  function machineKey(record) {
    const {tokenPair, edgeName} = identityOf(record);
    return tokenPair && edgeName ? JSON.stringify([tokenPair, edgeName]) : null;
  }

  function matches(record, tokenQuery, nameQuery) {
    const identity = identityOf(record);
    const tokenValue = fieldLabel(identity.tokenPair, identity).toLocaleLowerCase();
    const nameValue = fieldLabel(identity.edgeName, identity).toLocaleLowerCase();
    return tokenValue.includes(String(tokenQuery || "").trim().toLocaleLowerCase()) &&
      nameValue.includes(String(nameQuery || "").trim().toLocaleLowerCase());
  }

  function distinctMachines(records) {
    return new Set(records.map(machineKey).filter(Boolean)).size;
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  globalThis.EdgeIdentity = Object.freeze({
    identityOf, targetOf, targetLabel, fieldLabel, scopeKey, machineKey,
    matches, distinctMachines, formatTime,
  });
})();
