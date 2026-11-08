import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

let definitions;
let revision = 0;
let bootId = null;
let pending = [];
let updating = false;
let ready = false;

async function syncHead() {
  const response = await api.fetchApi("/magik_auto_nodes_refresh/state?since=head", { cache: "no-store" });
  if (!response.ok) throw new Error(`Could not read refresh head: ${response.status}`);
  const state = await response.json();
  bootId = state.boot_id;
  revision = state.revision;
}

const getNodeDefs = app.getNodeDefs;
app.getNodeDefs = async function (...args) {
  // Read the head first: anything newer is replayed (idempotently); anything older is already in the defs.
  await syncHead().catch((error) => console.warn("magik_auto_nodes_refresh:", error));
  definitions = await getNodeDefs.apply(this, args);
  return definitions;
};

function visitNodes(graph, callback, seen = new Set()) {
  // A subgraph definition is shared by all of its instances, so visit each graph once.
  if (!graph || seen.has(graph)) return;
  seen.add(graph);
  for (const node of graph.nodes ?? []) {
    if (node.isSubgraphNode?.() && node.subgraph) {
      visitNodes(node.subgraph, callback, seen);
    }
    callback(node);
  }
}

function containsChanged(graph, changed, seen = new Set()) {
  if (!graph || seen.has(graph)) return false;
  seen.add(graph);
  for (const node of graph.nodes ?? []) {
    if (changed[node.type]) return true;
    if (node.isSubgraphNode?.() && node.subgraph && containsChanged(node.subgraph, changed, seen)) return true;
  }
  return false;
}

// Promoted combo widgets on subgraph nodes keep an options snapshot in a frontend-internal store that
// extensions cannot reach; only the built-in full refresh re-syncs it.
function promotedComboStale(changed) {
  let stale = false;
  visitNodes(app.rootGraph, (node) => {
    if (stale || !node.isSubgraphNode?.() || !node.subgraph) return;
    if (!node.inputs?.some((input) => input.widgetId)) return;
    stale = containsChanged(node.subgraph, changed);
  });
  return stale;
}

// The built-in refresh also fires this hook so other extensions can update their own widgets.
async function invokeRefreshHooks(defs) {
  let disabled = [];
  try {
    disabled = app.ui?.settings?.getSettingValue?.("Comfy.Extension.Disabled", []) ?? [];
  } catch {
    disabled = [];
  }
  await Promise.all((app.extensions ?? []).map(async (extension) => {
    if (extension.name === "magik_auto_nodes_refresh" || disabled.includes(extension.name)) return;
    if (typeof extension.refreshComboInNodes !== "function") return;
    try {
      await extension.refreshComboInNodes(defs, app);
    } catch (error) {
      console.error(`magik_auto_nodes_refresh: ${extension.name} refreshComboInNodes failed:`, error);
    }
  }));
}

function comboValues(spec) {
  if (!Array.isArray(spec)) return;
  if (Array.isArray(spec[0])) return spec[0];
  if (spec[0] === "COMBO") return spec[1]?.options;
}

function updateWidgets(changed) {
  visitNodes(app.rootGraph, (node) => {
    try {
      node.refreshComboInNode?.(definitions ?? {});
    } catch (error) {
      console.error(`magik_auto_nodes_refresh: ${node.type} refreshComboInNode failed:`, error);
    }
    const def = changed[node.type];
    if (!def?.input) return;
    for (const widget of node.widgets ?? []) {
      if (widget.type !== "combo") continue;
      const spec = def.input.required?.[widget.name] ?? def.input.optional?.[widget.name];
      const values = comboValues(spec);
      if (values && widget.options) widget.options.values = values;
    }
  });
  app.canvas?.setDirty(true, true);
}

async function fullRefresh() {
  // Frontend builds differ in what they expose; use whichever full-refresh entry point exists.
  const refresh = app.reloadNodeDefs ?? app.refreshComboInNodes;
  if (typeof refresh !== "function") {
    throw new Error("no full-refresh method available on app; use the Refresh Node Definitions command");
  }
  await refresh.call(app);
}

async function applyEvents(events) {
  const nodeNames = new Set();
  for (const event of events) {
    for (const name of event.nodes ?? []) nodeNames.add(name);
  }
  if (!nodeNames.size) return;

  const changed = {};
  await Promise.all([...nodeNames].map(async (name) => {
    const response = await api.fetchApi(`/object_info/${encodeURIComponent(name)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Could not refresh ${name}: ${response.status}`);
    const info = (await response.json())[name];
    if (!info) throw new Error(`Missing definition for ${name}`);
    const previous = definitions?.[name];
    changed[name] = previous ? { ...info, display_name: previous.display_name, category: previous.category } : info;
  }));

  for (const [name, def] of Object.entries(changed)) {
    await app.registerNodeDef(name, def);
    if (definitions) definitions[name] = def;
  }
  updateWidgets(changed);
  if (promotedComboStale(changed)) {
    // The full refresh re-syncs promoted widgets, fires the extension hook and updates the Vue defs.
    await fullRefresh();
    return;
  }
  await invokeRefreshHooks(definitions ?? {});
  if (definitions && app.vueAppReady) app.updateVueAppNodeDefs(definitions);
}

async function drain() {
  if (updating) return;
  updating = true;
  try {
    while (pending.length) {
      const events = pending.filter((event) => event.revision > revision);
      pending = [];
      if (!events.length) continue;
      await applyEvents(events);
      revision = Math.max(revision, ...events.map((event) => event.revision));
    }
  } catch (error) {
    console.error("magik_auto_nodes_refresh:", error);
    pending = [];
    try {
      // The head must precede definitions. Never acknowledge changes after the snapshot.
      await syncHead();
      await fullRefresh();
      const response = await api.fetchApi(
        `/magik_auto_nodes_refresh/state?since=${revision}&boot=${encodeURIComponent(bootId)}`,
        { cache: "no-store" },
      );
      if (!response.ok) throw new Error(`Could not replay recovery events: ${response.status}`);
      const state = await response.json();
      if (state.full_refresh) throw new Error("Server changed during recovery; reconnect or refresh manually");
      pending.push(...state.events.filter((event) => event.revision > revision));
    } catch (fallbackError) {
      console.error("magik_auto_nodes_refresh: fallback refresh failed:", fallbackError);
    }
  } finally {
    updating = false;
    if (pending.length) void drain();
  }
}

async function reconcile() {
  if (bootId === null) {
    await syncHead();
    return;
  }
  const response = await api.fetchApi(
    `/magik_auto_nodes_refresh/state?since=${revision}&boot=${encodeURIComponent(bootId)}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw new Error(`Could not check refresh state: ${response.status}`);
  const state = await response.json();
  if (state.full_refresh) {
    await fullRefresh();
    bootId = state.boot_id;
    revision = state.revision;
    return;
  }
  pending.push(...state.events.filter((event) => event.revision > revision));
  await drain();
}

api.addEventListener("magik_auto_nodes_refresh", ({ detail }) => {
  if (bootId !== null && detail.boot_id !== bootId) {
    if (ready) void reconcile().catch(console.error);
    return;
  }
  if (detail.revision > revision) {
    pending.push(detail);
    if (ready) void drain();
  }
});
api.addEventListener("reconnected", () => {
  if (ready) void reconcile().catch(console.error);
});

app.registerExtension({
  name: "magik_auto_nodes_refresh",
  async setup() {
    ready = true;
    try {
      await reconcile();
    } catch (error) {
      console.error("magik_auto_nodes_refresh: initial reconcile failed:", error);
    }
  },
});
