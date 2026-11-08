# Implementation plan

The implementation uses `watchdog`'s native filesystem observer, records dependencies through `folder_paths` calls during node-definition generation, and sends affected node names over ComfyUI's WebSocket. The browser fetches each changed definition separately. The model sidebar and custom nodes that bypass `folder_paths` remain outside the automatic update path.

## Goal

After the initial node-definition load, a file creation, deletion, rename, or relevant modification should update only the node definitions and open widgets that depend on that file's folder. Keep ComfyUI's existing manual full refresh as a recovery path. Do not reload Python node code or change workflow values.

## Existing boundaries

- `server.py` builds every definition in `/object_info`, but `/object_info/{node_class}` can return one definition.
- `folder_paths.get_filename_list()` supplies most model-file combos. Its current cache checks directory timestamps and rescans a whole category after a change.
- The frontend's refresh command requests all definitions, re-registers every node, walks every graph node, and refreshes loaded model folders. Its node-definition store has an `addNodeDef` operation for a single definition; `updateNodeDefs` replaces the entire store.
- Some nodes, including core `LoadImage`, use direct directory listing. Arbitrary custom nodes may do the same or derive choices from more than one folder.

## Implemented

1. **Track relevant roots.** Build a filename index for configured model folders and the top level of the input folder at server startup. Subscribe through `watchdog.Observer` to native filesystem events and inspect directories affected by creates, deletes, and moves. Watch parents to handle missing or recreated roots, and subscribe separately to symlink targets. Handle nested folders, overlapping roots, extension filters, and duplicate relative names. Each path is scheduled on a running observer so a failure (such as the inotify watch limit) degrades only that path, to optional polling. Drop events outside the watched scope in the handler. Skip the subscription rebuild when a structural signature is unchanged. Adopt roots added after startup.
2. **Record dependencies.** During `/object_info`, record nodes that call `folder_paths.get_filename_list()`, `get_folder_paths()`, or `get_input_directory()`. This covers both V1 inputs and V3 schemas that use those functions (V3 `define_schema()` runs on every definition request, so refetched combos are current). The frame walk accepts any `server` frame with a `node_class` local, and a warning is logged if a full `/object_info` completes with nothing recorded. Nodes that bypass `folder_paths` retain manual refresh.
3. **Update file lists.** Apply additions, deletions, and renames to the watched index. Publish the resulting list through ComfyUI's existing filename-list cache, keeping `get_filename_list()` and model loading contracts unchanged. The cached mtime map also carries each root under its configured string, which core's cache check requires verbatim, and the cache is left alone if its tuple format changes. Only categories with changed names generate updates.
4. **Notify browsers.** Coalesce events for 200 ms with a one-second maximum delay, then send one WebSocket update with change counts, a boot ID and dependent node types. Each browser fetches `/object_info/{node_class}` for those types, re-registers only those definitions, and updates matching combo widgets in normal graphs and subgraphs. Preserve current widget selections. Mirror the built-in refresh's per-node `refreshComboInNode` call and `refreshComboInNodes` extension hook, and use the built-in full refresh when promoted subgraph widgets are affected.
5. **Recover.** Keep a bounded revision history so reconnecting browsers can replay missed changes. Browsers read the head revision before loading definitions and carry the server's boot ID. Fall back to ComfyUI's full refresh when the boot ID differs, the revision gap is too large, or a selective request fails. Start and stop the observer with the server lifecycle; reconcile indexes when registering new native watches.

## Verification and remaining checks

- Automated checks cover native create/delete/move events, idle behavior, missing and recreated roots, symlink targets and retargeting, extension filtering, duplicate names across roots, dependency tracing, revision gaps, targeted browser requests, subgraphs, and preserved selections.
- A live browser run and `/object_info` latency comparison remain to be done when ComfyUI is running, including Nodes 2.0 (Vue) mode.

## Limits

Custom nodes that scan files directly without `folder_paths` need an adapter. Changes to Python node implementations still require a ComfyUI restart. A complete initial node-definition load remains necessary for a new browser session. File-content changes without filename changes do not alter node choices and are not reported. The model sidebar must be refreshed with its own control. The frontend's node-definition store still reprocesses the cached full set after a selective update, although only affected definitions are fetched and re-registered.

## Magik fork verification

Twelve backend unit/regression tests passed with a minimal folder_paths API stub and real watchdog observers. The filesystem filter/rebuild verification passed. Both browser suites passed, including throwing per-node hooks and replay during error recovery. A live ComfyUI/Vue integration run remains necessary.
