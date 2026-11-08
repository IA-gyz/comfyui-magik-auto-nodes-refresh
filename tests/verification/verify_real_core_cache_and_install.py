import os, sys
PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMFY = os.environ.get("COMFYUI_ROOT", os.path.dirname(os.path.dirname(PKG)))  # ComfyUI root (needs folder_paths.py)
import sys, os, types, time, tempfile, logging, importlib, threading, errno
which = sys.argv[1] if len(sys.argv) > 1 else PKG   # folder containing the refresh.py to test
sys.path.insert(0, COMFY); sys.path.insert(0, which)
import folder_paths
from aiohttp import web
tmp = os.path.realpath(tempfile.mkdtemp())
models = os.path.join(tmp, "models"); inp = os.path.join(tmp, "input")
loras = os.path.join(models, "loras"); extra = os.path.join(tmp, "extra", "loras2")
for d in (loras, extra, inp): os.makedirs(d)
folder_paths.models_dir = models
folder_paths.set_input_directory(inp)
# non-normalized configured path (trailing slash): core's cache check needs this verbatim key
folder_paths.folder_names_and_paths["loras"] = ([loras + "/"], {".safetensors"})
events = []
srv = types.SimpleNamespace(routes=web.RouteTableDef(), app=web.Application(), send_sync=lambda n, e: events.append(e))
sv = types.ModuleType("server")
sv.PromptServer = type("P", (), {"instance": srv}); sys.modules["server"] = sv
import refresh
logs = []
class H(logging.Handler):
    def emit(self, r): logs.append(r.getMessage())
logging.getLogger().addHandler(H()); logging.getLogger().setLevel(logging.WARNING)

scans = {"n": 0}; orig_scan = folder_paths.get_filename_list_
def counting(name): scans["n"] += 1; return orig_scan(name)
folder_paths.get_filename_list_ = counting

import asyncio
patched = os.path.abspath(which) == PKG
base = len(srv.app.on_startup)
refresh.install(); after_first = len(srv.app.on_startup)
if patched:
    refresh.install()
    assert after_first == base + 1 and len(srv.app.on_startup) == after_first, "install guard failed"
fr = getattr(folder_paths, "_magik_auto_nodes_refresh", None)
if patched:
    print("INSTALL_GUARD_OK")
    ns = {"__name__": "server", "folder_paths": folder_paths}
    exec("def some_renamed_info(node_class):\n    return folder_paths.get_filename_list('loras')", ns)
    ns["some_renamed_info"]("LoraLoader")
    assert fr.dependencies.get("loras") == {"LoraLoader"}, fr.dependencies
    ns2 = {"__name__": "server", "folder_paths": folder_paths}
    exec("def i(node_class):\n    folder_paths.get_input_directory(); return folder_paths.get_filename_list('loras')", ns2); ns2["i"]("LoadImageX")
    assert fr.dependencies["input"] == {"LoadImageX"}
    print("RECORD_RELAXED_OK")
asyncio.run(srv.app.on_startup[-1](srv.app)); time.sleep(0.5)

def settle(t=1.7): time.sleep(t)

# item 13: with a trailing-slash configured path, core must still HIT our cache after an update
folder_paths.get_filename_list("loras")          # core scan populates its cache
open(os.path.join(loras, "a.safetensors"), "w").close(); settle()
scans["n"] = 0
result = folder_paths.get_filename_list("loras")
assert "a.safetensors" in result, result
print("CACHE_HIT" if scans["n"] == 0 else "CACHE_MISS", "core rescans after update:", scans["n"])
