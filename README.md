# Magik Auto Nodes Refresh

Automatically updates file-backed node dropdown choices after files are added, removed, or renamed.

## Install

1. Remove or disable the original `fast_node_refresh` extension. Do not run both together.
2. Extract `Magik_Auto_Nodes_Refresh` into `ComfyUI/custom_nodes/`.
3. Using ComfyUI's Python environment, run `python -m pip install -r custom_nodes/Magik_Auto_Nodes_Refresh/requirements.txt`.
4. Restart ComfyUI and reload the browser page.

Selections are preserved, including a selected filename that has been removed. Choose a valid replacement before running the workflow.

## Configuration

- `MAGIK_AUTO_NODES_REFRESH_POLL`: paths separated by the OS path separator (`:` on Linux) that need polling, or `off` to disable fallback polling.
- `MAGIK_AUTO_NODES_REFRESH_POLL_INTERVAL`: polling interval in seconds, default 10, minimum 1.
- Original `FAST_NODE_REFRESH_*` environment names remain supported as fallbacks.

Native events are debounced for 200 ms, with a one-second maximum batch delay. Failed subscriptions retry every 30 seconds. Explicit polling subscriptions are retained even inside a native recursive watch.

## Reliability changes

Recovery reads the revision before fetching definitions and replays subsequent changes. New categories report existing files; runtime extension-filter changes rebuild the category cache. A failing node refresh hook is isolated. Failed watches retry automatically. Protocol tests use change counts and boot IDs.

## Limits

Python node changes require restart. Nodes scanning folders without the wrapped `folder_paths` functions may still need manual refresh. Input watching covers top-level files and `input/3d`. File content changes without filename changes are ignored. The model sidebar has its own refresh.

Affected directory reads are selective, but category filename unions/sorting and the Vue definition-store update still process larger cached sets. Dependency detection relies on ComfyUI's server call stack. A real ComfyUI installation is required for final integration verification.

## Tests

From ComfyUI's root, using its Python environment:

```sh
python -B -m unittest discover -s custom_nodes/Magik_Auto_Nodes_Refresh/tests -v
node custom_nodes/Magik_Auto_Nodes_Refresh/tests/test_browser.mjs
node custom_nodes/Magik_Auto_Nodes_Refresh/tests/verification/verify_browser_extension.mjs
```

Based on [envy-ai/fast_node_refresh](https://github.com/envy-ai/fast_node_refresh). Original MIT license and copyright retained in LICENSE.
