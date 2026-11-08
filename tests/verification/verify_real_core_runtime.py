import os, sys
PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMFY = os.environ.get("COMFYUI_ROOT", os.path.dirname(os.path.dirname(PKG)))  # ComfyUI root (needs folder_paths.py)
import sys, os, types, time, tempfile, logging, errno, asyncio
os.environ["FAST_NODE_REFRESH_POLL_INTERVAL"] = "1"
sys.path.insert(0, COMFY); sys.path.insert(0, PKG)
import folder_paths
from aiohttp import web
tmp = os.path.realpath(tempfile.mkdtemp())
models = os.path.join(tmp, "models"); inp = os.path.join(tmp, "input")
loras = os.path.join(models, "loras"); late = os.path.join(tmp, "late"); direct = os.path.join(tmp, "direct"); failing = os.path.join(tmp, "failing")
for d in (loras, late, direct, failing, inp): os.makedirs(d)
open(os.path.join(late, "c.safetensors"), "w").close()
open(os.path.join(direct, "d.safetensors"), "w").close()
open(os.path.join(failing, "f.safetensors"), "w").close()
folder_paths.models_dir = models; folder_paths.set_input_directory(inp)
folder_paths.folder_names_and_paths["loras"] = ([loras], {".safetensors"})
events = []
srv = types.SimpleNamespace(routes=web.RouteTableDef(), app=web.Application(), send_sync=lambda n, e: events.append(e))
sv = types.ModuleType("server"); sv.PromptServer = type("P", (), {"instance": srv}); sys.modules["server"] = sv
import refresh
logs = []
class H(logging.Handler):
    def emit(self, r): logs.append(r.getMessage())
logging.getLogger().addHandler(H()); logging.getLogger().setLevel(logging.WARNING)
refresh.install(); fr = folder_paths._magik_auto_nodes_refresh
fr.dependencies = {"loras": {"LoraLoader"}, "input": {"LoadLatent"}}
asyncio.run(srv.app.on_startup[-1](srv.app)); time.sleep(0.5)
def settle(t=1.8): time.sleep(t)
def touch(*p): open(os.path.join(*p), "w").close()

# 14a: root added at runtime via add_model_folder_path
n = len(events); folder_paths.add_model_folder_path("loras", late); settle()
assert len(events) == n + 1 and events[-1]["categories"]["loras"]["added"] == 1 and events[-1]["nodes"] == ["LoraLoader"], events[-1:]
touch(late, "c2.safetensors"); settle()
assert len(events) == n + 2, "late root not watched"
assert "c.safetensors" in folder_paths.get_filename_list("loras") and "c2.safetensors" in folder_paths.get_filename_list("loras")
print("LATE_ROOT_OK")

# 14b: root appended by mutating folder_names_and_paths directly; noticed at the next /object_info
n = len(events); folder_paths.folder_names_and_paths["loras"][0].append(direct); settle(0.6)
assert len(events) == n, "should not be noticed without a trigger"
asyncio.run(srv.app.on_response_prepare[-1](types.SimpleNamespace(path="/object_info"), types.SimpleNamespace(status=200))); settle()
assert len(events) == n + 1 and events[-1]["categories"]["loras"]["added"] == 1, events[-1:]
print("DIRECT_MUTATION_PICKED_UP_ON_OBJECT_INFO_OK")

# 7: warning fires once when nothing was recorded
saved = fr.dependencies; fr.dependencies = {}
hook = srv.app.on_response_prepare[-1]
for _ in range(3): asyncio.run(hook(types.SimpleNamespace(path="/object_info"), types.SimpleNamespace(status=200)))
assert sum("no node dependencies were recorded" in m for m in logs) == 1, logs
fr.dependencies = saved
print("EMPTY_DEPENDENCY_WARNING_OK")

# 12/15: inotify failure on a new root -> automatic polling fallback, other watches unaffected
real_schedule = fr.observer.schedule
def failing_schedule(handler, path, recursive=False):
    if path.startswith(failing) or path == os.path.realpath(failing):
        raise OSError(errno.ENOSPC, "inotify watch limit reached")
    return real_schedule(handler, path, recursive=recursive)
fr.observer.schedule = failing_schedule
n = len(events); folder_paths.add_model_folder_path("loras", failing); settle()
assert len(events) == n + 1 and events[-1]["categories"]["loras"]["added"] == 1
assert any("polling every" in m and "max_user_watches" in m for m in logs), logs
touch(failing, "f2.safetensors"); time.sleep(3.2)              # polled at 1s
assert len(events) >= n + 2 and events[-1]["categories"]["loras"]["added"] >= 1, "polling fallback did not deliver"
touch(loras, "still_native.safetensors"); settle()
assert "still_native.safetensors" in folder_paths.get_filename_list("loras")
print("INOTIFY_FAILURE_POLL_FALLBACK_OK")

# input handling: .latent counts; hidden/partial names don't; input/3d subfolder maps to input dependents
n = len(events); touch(inp, "x.latent"); settle()
assert len(events) == n + 1 and events[-1]["nodes"] == ["LoadLatent"] and "input" in events[-1]["categories"], events[-1:]
n = len(events); touch(inp, ".hidden"); touch(inp, "big.png.part"); settle()
assert len(events) == n, "ignored names must not notify"
n = len(events); os.makedirs(os.path.join(inp, "3d")); touch(inp, "3d", "m.glb"); settle()
assert len(events) > n and "input3d" in events[-1]["categories"] and events[-1]["nodes"] == ["LoadLatent"], events[-1:]
print("INPUT_HANDLING_OK")
fr.stop(); print("ALL_OK")
