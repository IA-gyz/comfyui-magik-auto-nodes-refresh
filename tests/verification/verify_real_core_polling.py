import os, sys
PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMFY = os.environ.get("COMFYUI_ROOT", os.path.dirname(os.path.dirname(PKG)))  # ComfyUI root (needs folder_paths.py)
import sys, os, types, time, tempfile, logging, errno, asyncio
mode = sys.argv[1]
os.environ["FAST_NODE_REFRESH_POLL_INTERVAL"] = "1"
tmp = os.path.realpath(tempfile.mkdtemp()); polled = os.path.join(tmp, "nas", "loras")
os.makedirs(polled)
os.environ["FAST_NODE_REFRESH_POLL"] = polled if mode == "explicit" else "off"
sys.path.insert(0, COMFY); sys.path.insert(0, PKG)
import folder_paths
from aiohttp import web
models = os.path.join(tmp, "models"); inp = os.path.join(tmp, "input"); os.makedirs(models); os.makedirs(inp)
folder_paths.models_dir = models; folder_paths.set_input_directory(inp)
folder_paths.folder_names_and_paths["loras"] = ([polled], {".safetensors"})
events = []
srv = types.SimpleNamespace(routes=web.RouteTableDef(), app=web.Application(), send_sync=lambda n, e: events.append(e))
sv = types.ModuleType("server"); sv.PromptServer = type("P", (), {"instance": srv}); sys.modules["server"] = sv
import refresh
logs = []
class H(logging.Handler):
    def emit(self, r): logs.append(r.getMessage())
logging.getLogger().addHandler(H())
refresh.install(); fr = folder_paths._magik_auto_nodes_refresh; fr.dependencies = {"loras": {"L"}}
asyncio.run(srv.app.on_startup[-1](srv.app)); time.sleep(0.5)
if mode == "explicit":
    assert fr.poller is not None and any(k[0] == polled for k, (obs, w) in fr.subscriptions.items() if obs is fr.poller), "explicit path not polled"
    open(os.path.join(polled, "n.safetensors"), "w").close(); time.sleep(3.2)
    assert events and events[-1]["categories"]["loras"]["added"] == 1, events
    print("EXPLICIT_POLL_OK")
else:
    real = fr.observer.schedule
    def boom(h, path, recursive=False): raise OSError(errno.ENOSPC, "limit")
    fr.observer.schedule = boom
    new = os.path.join(tmp, "new"); os.makedirs(new)
    folder_paths.add_model_folder_path("loras", new); time.sleep(1.8)
    assert fr.poller is None and any("cannot watch" in m and "manual refresh" in m for m in logs), logs
    print("POLL_OFF_DEGRADES_TO_MANUAL_OK")
fr.stop()
