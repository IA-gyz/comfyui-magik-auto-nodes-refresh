import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../web/magik_auto_nodes_refresh.js", import.meta.url), "utf8")
  .replace(/^import .*;\n/gm, "");
const listeners = {};
const requests = [];
const registered = [];
const loader = { type: "Loader", widgets: [{ type: "combo", name: "model", value: "old.pt", options: { values: ["old.pt"] } }] };
const other = { type: "Other", widgets: [{ type: "combo", name: "choice", options: { values: ["keep"] } }] };
const nested = { isSubgraphNode: () => true, subgraph: { nodes: [loader] } };
const app = {
  rootGraph: { nodes: [nested, other] },
  canvas: { setDirty() {} },
  vueAppReady: true,
  async getNodeDefs() {
    return {
      Loader: { name: "Loader", input: { required: { model: [["old.pt"]] } } },
      Other: { name: "Other", input: { required: { choice: [["keep"]] } } },
    };
  },
  async registerNodeDef(name) { registered.push(name); },
  updateVueAppNodeDefs() {},
  registerExtension(extension) { this.extension = extension; },
};
const api = {
  addEventListener(name, handler) { listeners[name] = handler; },
  async fetchApi(path) {
    requests.push(path);
    if (path.startsWith("/magik_auto_nodes_refresh/state")) return { ok: true, json: async () => ({ boot_id: "test", revision: 0, events: [] }) };
    if (path === "/object_info/Loader") return { ok: true, json: async () => ({ Loader: { name: "Loader", input: { required: { model: [["old.pt", "new.pt"]] } } } }) };
    throw new Error(`Unexpected request: ${path}`);
  },
};
runInNewContext(source, { app, api, console, setTimeout });
await app.getNodeDefs();
await app.extension.setup();
listeners.magik_auto_nodes_refresh({ detail: { boot_id: "test", revision: 1, categories: { test_models: { added: ["new.pt"], removed: [] } }, nodes: ["Loader"] } });
await new Promise((resolve) => setTimeout(resolve, 0));

assert.deepEqual(registered, ["Loader"]);
assert.deepEqual(requests, ["/magik_auto_nodes_refresh/state?since=head", "/magik_auto_nodes_refresh/state?since=0&boot=test", "/object_info/Loader"]);
assert.deepEqual([...loader.widgets[0].options.values], ["old.pt", "new.pt"]);
assert.equal(loader.widgets[0].value, "old.pt");
assert.deepEqual(other.widgets[0].options.values, ["keep"]);
