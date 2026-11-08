import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// The extension imports ../../scripts/app.js and api.js; build a throwaway tree with stubs for them.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = fs.mkdtempSync(path.join(os.tmpdir(), "fnr-js-"));
fs.mkdirSync(path.join(root, "scripts")); fs.mkdirSync(path.join(root, "ext/fnr"), { recursive: true });
fs.writeFileSync(path.join(root, "package.json"), '{"type":"module"}');
fs.writeFileSync(path.join(root, "scripts/app.js"), `export const app = new Proxy({}, {
  get: (_, key) => globalThis.__app[key],
  set: (_, key, value) => { globalThis.__app[key] = value; return true; },
});`);
fs.writeFileSync(path.join(root, "scripts/api.js"), `export const api = {
  handlers: {},
  addEventListener(name, fn) { (this.handlers[name] ??= []).push(fn); },
  emit(name, detail) { for (const fn of this.handlers[name] ?? []) fn({ detail }); },
  async fetchApi(url) { return globalThis.__fetch(url); },
};`);
fs.copyFileSync(path.join(here, "../../web/magik_auto_nodes_refresh.js"), path.join(root, "ext/fnr/magik_auto_nodes_refresh.js"));
const url = (rel) => pathToFileURL(path.join(root, rel)).href;
let n = 0;
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const json = (body) => ({ ok: true, status: 200, json: async () => body });
async function scenario(name, build, fn) {
  const log = [];
  const S = build(log);
  globalThis.__app = S.app; globalThis.__fetch = S.fetch;
  const { api } = await import(url("scripts/api.js")); api.handlers = {};
  await import(`${url("ext/fnr/magik_auto_nodes_refresh.js")}?${++n}`);
  await S.app.getNodeDefs();              // what app.setup does
  await S.ext.setup();
  await fn(S, api, log);
  console.log("ok -", name);
}
function make(overrides = {}) {
  return (log) => {
    const S = { registered: [], vue: 0, reloads: 0, hookCalls: [], nodeRefresh: {} };
    S.ext = null;
    S.defs = { LoraLoader: { input: { required: { lora_name: [["old.safetensors"]] } }, display_name: "Load LoRA", category: "loaders" } };
    S.state = { boot_id: "B1", revision: 5, events: [] };
    S.app = {
      vueAppReady: true, extensions: [], ui: { settings: { getSettingValue: () => ["disabled_ext"] } },
      canvas: { setDirty() {} },
      async getNodeDefs() { log.push("getNodeDefs"); return S.defs; },
      async registerNodeDef(name, def) { S.registered.push(name); },
      async reloadNodeDefs() { S.reloads++; await this.getNodeDefs(); },
      updateVueAppNodeDefs() { S.vue++; },
      registerExtension(ext) { S.ext = ext; },
      rootGraph: { nodes: [] },
    };
    S.fetch = async (url) => {
      log.push(url);
      if (url.startsWith("/magik_auto_nodes_refresh/state")) return json(S.state);
      if (url.startsWith("/object_info/LoraLoader")) return json({ LoraLoader: { input: { required: { lora_name: [["new.safetensors", "x.safetensors"]] } }, display_name: "SERVER", category: "server" } });
      throw new Error("unexpected " + url);
    };
    const mkNode = (type, id) => ({ type, id, widgets: [{ type: "combo", name: "lora_name", options: { values: ["old.safetensors"] } }],
      refreshComboInNode() { S.nodeRefresh[id] = (S.nodeRefresh[id] ?? 0) + 1; } });
    S.mkNode = mkNode;
    Object.assign(S, overrides);
    return S;
  };
}

await scenario("head is read BEFORE /object_info-backed getNodeDefs; fresh page does not replay", make(), async (S, api, log) => {
  assert.deepEqual(log.slice(0, 2), ["/magik_auto_nodes_refresh/state?since=head", "getNodeDefs"]);
  assert.ok(!log.some((l) => l.includes("since=0")), log.join());
  assert.equal(S.reloads, 0);
});

await scenario("selective update: widgets, refreshComboInNode on ALL nodes, hooks, vue", make(), async (S, api) => {
  const lora = S.mkNode("LoraLoader", "a"), other = S.mkNode("KSampler", "b");
  S.app.rootGraph.nodes = [lora, other];
  S.app.extensions = [
    { name: "magik_auto_nodes_refresh", refreshComboInNodes: () => S.hookCalls.push("SELF") },
    { name: "other_pack", refreshComboInNodes: (defs, app) => S.hookCalls.push(["other_pack", Object.keys(defs), app.vueAppReady === true]) },
    { name: "disabled_ext", refreshComboInNodes: () => S.hookCalls.push("DISABLED") },
    { name: "broken_pack", refreshComboInNodes: () => { throw new Error("boom"); } },
    { name: "no_hook" },
  ];
  api.emit("magik_auto_nodes_refresh", { boot_id: "B1", revision: 6, nodes: ["LoraLoader"] }); await tick(80);
  assert.deepEqual(lora.widgets[0].options.values, ["new.safetensors", "x.safetensors"]);
  assert.deepEqual(other.widgets[0].options.values, ["old.safetensors"], "unrelated node untouched");
  assert.equal(S.nodeRefresh.a, 1, "changed-def node must also get refreshComboInNode");
  assert.equal(S.nodeRefresh.b, 1);
  assert.deepEqual(S.registered, ["LoraLoader"]);
  assert.deepEqual(S.hookCalls, [["other_pack", ["LoraLoader"], true]], JSON.stringify(S.hookCalls));
  assert.equal(S.vue, 1); assert.equal(S.reloads, 0);
  assert.equal(S.defs.LoraLoader.display_name, "Load LoRA", "display_name/category from the previous def are preserved");
  assert.deepEqual(S.defs.LoraLoader.input.required.lora_name[0], ["new.safetensors", "x.safetensors"], "defs cache updated in place");
});

await scenario("shared subgraph visited once", make(), async (S, api) => {
  const inner = S.mkNode("LoraLoader", "inner");
  const sub = { nodes: [inner] };
  const host = (id) => ({ type: "Subgraph", id, widgets: [], inputs: [], isSubgraphNode: () => true, subgraph: sub, refreshComboInNode() {} });
  S.app.rootGraph.nodes = [host("h1"), host("h2")];
  api.emit("magik_auto_nodes_refresh", { boot_id: "B1", revision: 6, nodes: ["LoraLoader"] }); await tick(80);
  assert.equal(S.nodeRefresh.inner, 1, "inner node visited " + S.nodeRefresh.inner);
});

await scenario("promoted combo on a subgraph node -> built-in full refresh instead", make(), async (S, api) => {
  const inner = S.mkNode("LoraLoader", "inner");
  const host = { type: "Subgraph", widgets: [], inputs: [{ widgetId: "w1" }], isSubgraphNode: () => true, subgraph: { nodes: [inner] }, refreshComboInNode() {} };
  S.app.rootGraph.nodes = [host];
  S.app.extensions = [{ name: "other_pack", refreshComboInNodes: () => S.hookCalls.push("hook") }];
  api.emit("magik_auto_nodes_refresh", { boot_id: "B1", revision: 6, nodes: ["LoraLoader"] }); await tick(80);
  assert.equal(S.reloads, 1); assert.equal(S.hookCalls.length, 0, "hooks come from the built-in refresh, not us"); assert.equal(S.vue, 0);
  // subgraph WITHOUT promoted inputs stays selective
  host.inputs = []; S.reloads = 0;
  api.emit("magik_auto_nodes_refresh", { boot_id: "B1", revision: 7, nodes: ["LoraLoader"] }); await tick(80);
  assert.equal(S.reloads, 0); assert.equal(S.hookCalls.length, 1);
});

await scenario("event from another boot -> reconcile -> full refresh; revision/boot adopted", make(), async (S, api) => {
  S.state = { boot_id: "B2", revision: 1, full_refresh: true };
  api.emit("magik_auto_nodes_refresh", { boot_id: "B2", revision: 1, nodes: ["LoraLoader"] }); await tick(80);
  assert.equal(S.reloads, 1);
  S.state = { boot_id: "B2", revision: 2, events: [{ boot_id: "B2", revision: 2, nodes: [] }] };
  S.fetchedBoot = null;
  const before = S.registered.length;
  api.emit("reconnected", {}); await tick(80);
  assert.equal(S.registered.length, before, "no-op events must not re-register");
});

await scenario("no reloadNodeDefs -> falls back to refreshComboInNodes", make(), async (S, api) => {
  let called = 0; delete S.app.reloadNodeDefs; S.app.refreshComboInNodes = async function () { called++; };
  S.state = { boot_id: "B9", revision: 1, full_refresh: true };
  api.emit("reconnected", {}); await tick(80);
  assert.equal(called, 1);
});

await scenario("neither full-refresh method + failing batch: logs, never throws", make(), async (S, api) => {
  delete S.app.reloadNodeDefs; const errors = []; const orig = console.error; console.error = (...a) => errors.push(a.join(" "));
  const oldFetch = S.fetch; globalThis.__fetch = async (u) => (u.startsWith("/object_info") ? { ok: false, status: 500, json: async () => ({}) } : oldFetch(u));
  process.on("unhandledRejection", (e) => { throw e; });
  api.emit("magik_auto_nodes_refresh", { boot_id: "B1", revision: 6, nodes: ["LoraLoader"] }); await tick(120);
  console.error = orig;
  assert.ok(errors.some((e) => e.includes("fallback refresh failed")), errors.join("|"));
});

await scenario("setup survives a failing initial reconcile", (log) => { const S = make()(log); return S; }, async () => {});
{ // reconcile failure at setup, with head unknown
  const S = make()([]); globalThis.__app = S.app;
  S.fetch = async () => { throw new Error("network down"); }; globalThis.__fetch = S.fetch;
  const { api } = await import(url("scripts/api.js")); api.handlers = {};
  await import(`${url("ext/fnr/magik_auto_nodes_refresh.js")}?${++n}`);
  await S.app.getNodeDefs();                 // head fetch fails -> warn, still loads defs
  const orig = console.error; let logged = 0; console.error = () => logged++;
  await S.ext.setup();                       // must not throw
  console.error = orig; assert.equal(logged, 1);
  console.log("ok - head/reconcile failures never break startup");
}
await scenario("throwing per-node hook does not abort widget updates", make(), async (S, api) => {
  const node = S.mkNode("LoraLoader", "bad");
  node.refreshComboInNode = () => { throw new Error("test hook failure"); };
  S.app.rootGraph.nodes = [node];
  api.emit("magik_auto_nodes_refresh", { boot_id: "B1", revision: 6, nodes: ["LoraLoader"] });
  await tick(80);
  assert.deepEqual(node.widgets[0].options.values, ["new.safetensors", "x.safetensors"]);
  assert.equal(S.reloads, 0);
});
await scenario("recovery replays a change arriving during full refresh", make(), async (S, api) => {
  const oldFetch = S.fetch;
  let failed = false;
  globalThis.__fetch = async (u) => {
    if (u.startsWith("/object_info/") && !failed) { failed = true; return { ok: false, status: 500 }; }
    return oldFetch(u);
  };
  S.app.reloadNodeDefs = async function () {
    S.reloads++;
    await this.getNodeDefs();
    S.state = { boot_id: "B1", revision: 7, events: [{ boot_id: "B1", revision: 7, nodes: ["LoraLoader"] }] };
  };
  api.emit("magik_auto_nodes_refresh", { boot_id: "B1", revision: 6, nodes: ["LoraLoader"] });
  await tick(120);
  assert.equal(S.reloads, 1);
  assert.deepEqual(S.registered, ["LoraLoader"]);
});
console.log("ALL_JS_OK");
