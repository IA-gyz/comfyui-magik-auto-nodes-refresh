import os, sys
PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMFY = os.environ.get("COMFYUI_ROOT", os.path.dirname(os.path.dirname(PKG)))  # ComfyUI root (needs folder_paths.py)
import sys, types, os, tempfile, time, logging, shutil
sys.path.insert(0, PKG)
logging.disable(logging.CRITICAL)
tmp = os.path.realpath(tempfile.mkdtemp())
comfy = os.path.join(tmp, "comfy"); models = os.path.join(comfy, "models")
loras = os.path.join(models, "loras"); extra = os.path.join(tmp, "extra", "loras2"); inp = os.path.join(comfy, "input")
for d in (loras, extra, inp): os.makedirs(d)
fp = types.ModuleType("folder_paths")
fp.models_dir = models
fp.folder_names_and_paths = {"loras": ([loras, extra], {".safetensors"})}
fp.get_input_directory = lambda: inp
fp.filter_files_content_types = lambda n, t: n
fp.map_legacy = lambda x: x
fp.filename_list_cache = {}
sys.modules["folder_paths"] = fp
sv = types.ModuleType("server"); sv.PromptServer = type("P", (), {"instance": None}); sys.modules["server"] = sv
import refresh

sent = []; processed = []
r = refresh.FileRefresh(types.SimpleNamespace(send_sync=lambda n, e: sent.append(e)))
r.dependencies = {"loras": {"LoraLoader"}}
orig = r.process
r.process = lambda paths, *a: (processed.append(set(paths)), orig(paths, *a))[1]
r.start()
def settle(): time.sleep(1.6)
def touch(*p): open(os.path.join(*p), "w").close()

# --- item 11: noise filtering ---
touch(comfy, "comfyui.log"); touch(models, "readme.txt"); touch(tmp, "extra", "stray.txt")
settle()
assert not processed, ("irrelevant file events queued work", processed)
print("NOISE_FILTERED_OK")

# relevant events still work: root, nested new dir, extra root outside models_dir
touch(loras, "a.safetensors"); settle()
assert sent and sent[-1]["categories"]["loras"] == {"added": 1, "removed": 0}, sent
os.makedirs(os.path.join(loras, "new")); touch(loras, "new", "b.safetensors"); settle()
assert sent[-1]["categories"]["loras"]["added"] >= 1 and len(sent) >= 2
n = len(sent); touch(extra, "c.safetensors"); settle()
assert len(sent) == n + 1, "extra root outside models_dir not detected"
print("RELEVANT_EVENTS_OK")

# --- root renamed away then recreated as a real dir (parent watch must catch it) ---
n = len(sent)
os.rename(extra, extra + "_old"); settle()
assert len(sent) == n + 1 and sent[-1]["categories"]["loras"]["removed"] == 1, sent[-1]
os.makedirs(extra); touch(extra, "d.safetensors"); settle()
assert len(sent) == n + 2 and sent[-1]["categories"]["loras"]["added"] == 1, sent[-1]
touch(extra, "e.safetensors"); settle()      # new watch on recreated root works
assert len(sent) == n + 3, "recreated root not watched"
print("ROOT_RENAME_RECREATE_OK")

# --- root recreated as a SYMLINK while missing (file-type event; must pass the filter) ---
n = len(sent)
shutil.rmtree(extra); settle()
target = os.path.join(tmp, "elsewhere"); os.makedirs(target); touch(target, "f.safetensors")
os.symlink(target, extra); settle()
assert len(sent) > n and sent[-1]["categories"]["loras"]["added"] >= 1, sent[-1]
print("SYMLINK_ROOT_OK")

# --- item 10: unchanged structure => no realpath/islink work ---
calls = {"n": 0}; real = os.path.realpath
def counting(*a, **k): calls["n"] += 1; return real(*a, **k)
os.path.realpath = counting
try:
    r.signature_cache = None; r.update_subscriptions(); rebuilt = calls["n"]
    calls["n"] = 0; r.update_subscriptions(); skipped = calls["n"]
finally:
    os.path.realpath = real
assert rebuilt > 5 and skipped <= 1, (rebuilt, skipped)  # 1 = the missing input/3d root
print(f"SKIP_REBUILD_OK rebuild_realpath_calls={rebuilt} unchanged_pass={skipped}")

# a structural change (new dir) must invalidate the cache
os.makedirs(os.path.join(loras, "another")); settle()
assert r.signature_cache is not None
r.stop(); print("ALL_OK")
