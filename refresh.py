import errno
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

from aiohttp import web
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

import folder_paths
from server import PromptServer


INPUT_CATEGORIES = ("input", "input3d")
DEPENDS_ON = {"input3d": "input"}
IGNORED_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", "~")


def ignored_input_name(name):
    base = os.path.basename(name)
    return base.startswith(".") or base.lower().endswith(IGNORED_SUFFIXES)


@dataclass
class Directory:
    identity: tuple[int, int]
    mtime: float
    files: set[str]
    children: set[str]


class RootWatch:
    def __init__(self, path, extensions, recursive=True):
        self.configured = path
        self.path = os.path.abspath(path)
        self.extensions = set(extensions)
        self.recursive = recursive
        self.directories = {}
        self._add(self.path, set())

    def _read(self, path):
        try:
            stat = os.stat(path)
            files = set()
            children = set()
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name == ".git":
                        continue
                    if entry.is_dir(follow_symlinks=True):
                        if self.recursive:
                            children.add(entry.path)
                    elif entry.is_file(follow_symlinks=True):
                        if not self.extensions or entry.name in self.extensions or os.path.splitext(entry.name)[1].lower() in self.extensions:
                            files.add(entry.name)
            return Directory((stat.st_dev, stat.st_ino), stat.st_mtime, files, children)
        except OSError:
            return None

    def _add(self, path, ancestors):
        real_path = os.path.realpath(path)
        if real_path in ancestors:
            return
        entry = self._read(path)
        if entry is None:
            return
        self.directories[path] = entry
        for child in entry.children:
            self._add(child, ancestors | {real_path})

    def _remove(self, path):
        prefix = path + os.sep
        for name in list(self.directories):
            if name == path or name.startswith(prefix):
                del self.directories[name]

    def refresh(self, paths):
        for path in sorted(paths, key=len):
            old = self.directories.get(path)
            new = self._read(path)
            if new is None:
                self._remove(path)
                continue
            ancestors = {os.path.realpath(parent) for parent in self._parents(path) if parent != path}
            if old is None or old.identity != new.identity:
                self._remove(path)
                self._add(path, ancestors)
                continue
            for child in old.children - new.children:
                self._remove(child)
            self.directories[path] = new
            for child in new.children:
                previous = self.directories.get(child)
                try:
                    stat = os.stat(child)
                except OSError:
                    self._remove(child)
                    continue
                if previous is None or previous.identity != (stat.st_dev, stat.st_ino):
                    self._remove(child)
                    self._add(child, ancestors | {os.path.realpath(path)})

    def _parents(self, path):
        while contains(self.path, path):
            yield path
            if path == self.path:
                break
            path = os.path.dirname(path)

    def names(self):
        return {
            os.path.relpath(os.path.join(path, name), self.path)
            for path, entry in self.directories.items()
            for name in entry.files
        }

    def mtimes(self):
        values = {path: entry.mtime for path, entry in self.directories.items()}
        root = self.directories.get(self.path)
        if root is not None:
            # Core's cache check looks up each configured root string verbatim.
            values[self.configured] = root.mtime
        return values


_warned = set()


def warn_once(key, message, *args):
    if key not in _warned:
        _warned.add(key)
        logging.warning(message, *args)


def cache_compatible():
    """True if folder_paths.filename_list_cache still holds (list, dict, float) tuples."""
    cache = getattr(folder_paths, "filename_list_cache", None)
    if not isinstance(cache, dict):
        return False
    return all(
        isinstance(value, tuple) and len(value) == 3 and isinstance(value[0], list) and isinstance(value[1], dict)
        for value in cache.values()
    )


class CategoryWatch:
    def __init__(self, name, paths, extensions, recursive=True, cache=True):
        self.name = name
        self.extensions = set(extensions)
        self.recursive = recursive
        self.roots = [RootWatch(path, extensions, recursive) for path in paths]
        self.files = self.names()
        self.cache = cache

    def sync(self, paths):
        """Adopt a changed list of configured roots; returns True if it changed."""
        wanted = list(paths)
        if [root.configured for root in self.roots] == wanted:
            return False
        known = {root.configured: root for root in self.roots}
        self.roots = [known.get(path) or RootWatch(path, self.extensions, self.recursive) for path in wanted]
        return True

    def names(self):
        return set().union(*(root.names() for root in self.roots))

    def refresh(self, directories):
        for root, paths in directories.items():
            root.refresh(paths)
        current = self.names()
        added = current - self.files
        removed = self.files - current
        self.files = current
        mtimes = {}
        for root in self.roots:
            mtimes.update(root.mtimes())
        if self.cache:
            if cache_compatible():
                folder_paths.filename_list_cache[self.name] = (sorted(current), mtimes, time.perf_counter())
            else:
                warn_once("cache", "magik_auto_nodes_refresh: folder_paths.filename_list_cache has an unexpected format; not updating it")
        if not added and not removed:
            return None
        return {"added": sorted(added), "removed": sorted(removed)}


def contains(directory, path):
    directory = os.path.normcase(os.path.abspath(directory))
    path = os.path.normcase(os.path.abspath(path))
    try:
        return os.path.commonpath((directory, path)) == directory
    except ValueError:
        return False


def ancestors(path):
    path = os.path.normcase(os.path.abspath(path))
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def existing_parent(path):
    path = os.path.abspath(path)
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    return os.path.realpath(path)


class FileRefresh(FileSystemEventHandler):
    def __init__(self, server):
        self.server = server
        self.dependencies = {}
        self.history = deque(maxlen=100)
        self.revision = 0
        self.boot_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.condition = threading.Condition()
        self.stopping = False
        self.pending = set()
        self.invalidated = set()
        self.last_event = 0
        self.thread = None
        self.watches = {}
        self.directories = {}
        self.observer = Observer()
        self.subscriptions = {}
        self.failed = {}  # subscription -> monotonic retry deadline
        self.starting = False
        self.resync = False
        self.poller = None
        setting = os.environ.get("MAGIK_AUTO_NODES_REFRESH_POLL", os.environ.get("FAST_NODE_REFRESH_POLL", "")).strip()
        self.poll_off = setting.lower() == "off"
        self.poll_paths = [] if self.poll_off else [os.path.abspath(path) for path in setting.split(os.pathsep) if path.strip()]
        try:
            self.poll_interval = max(1.0, float(os.environ.get("MAGIK_AUTO_NODES_REFRESH_POLL_INTERVAL", os.environ.get("FAST_NODE_REFRESH_POLL_INTERVAL", "10"))))
        except ValueError:
            self.poll_interval = 10.0
        self.signature_cache = None
        self.scope_prefixes = ()
        self.interest = set()

    def configured_categories(self):
        for name, (paths, extensions) in list(folder_paths.folder_names_and_paths.items()):
            if name not in ("custom_nodes", "datasets"):
                yield name, list(paths), extensions

    def make_watch(self, name, paths, extensions):
        if name == "diffusers":
            return CategoryWatch(name, paths, {"model_index.json"}, cache=False)
        return CategoryWatch(name, paths, extensions)

    def start(self):
        for name, paths, extensions in self.configured_categories():
            self.watches[name] = self.make_watch(name, paths, extensions)
        input_dir = folder_paths.get_input_directory()
        self.watches["input"] = CategoryWatch("input", [input_dir], set(), recursive=False, cache=False)
        self.watches["input3d"] = CategoryWatch("input3d", [os.path.join(input_dir, "3d")], set(), recursive=False, cache=False)
        # Start first so each path is scheduled on a live observer: a failure (for example the inotify
        # watch limit) then affects only that path instead of aborting every watch.
        self.observer.start()
        self.starting = True  # the gap-closing scan below covers what a queued reconciliation would
        try:
            self.update_subscriptions()
        finally:
            self.starting = False
        # Close the gap between the initial index and native watch registration.
        for watch in self.watches.values():
            watch.refresh({root: set(root.directories) | {root.path} for root in watch.roots})
        self.update_subscriptions()
        self.thread = threading.Thread(target=self.run, name="magik_auto_nodes_refresh", daemon=True)
        self.thread.start()

    def stop(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
        for observer in (self.observer, self.poller):
            if observer is not None:
                observer.stop()
                if observer.is_alive():
                    observer.join()

    def request_resync(self):
        with self.condition:
            self.resync = True
            self.last_event = time.monotonic()
            self.condition.notify()

    def sync_roots(self):
        """Adopt folder roots added or removed after startup; returns {category: changes}."""
        results = {}
        for name, paths, extensions in self.configured_categories():
            watch = self.watches.get(name)
            if watch is None:
                watch = self.make_watch(name, paths, extensions)
                self.watches[name] = watch
                results[name] = {"added": sorted(watch.files), "removed": []}
            elif watch.extensions != ({"model_index.json"} if name == "diffusers" else extensions):
                replacement = self.make_watch(name, paths, extensions)
                replacement.files = watch.files
                self.watches[name] = replacement
                changes = replacement.refresh({})
                # Definitions can change even if the resulting filename set is identical.
                results[name] = changes or {"added": [], "removed": []}
            elif watch.sync(paths):
                changes = watch.refresh({})
                if changes is not None:
                    results[name] = changes
        return results

    def polled(self, path):
        return any(contains(base, path) for base in self.poll_paths)

    def poller_observer(self):
        if self.poller is None:
            self.poller = PollingObserver(timeout=self.poll_interval)
            self.poller.start()
        return self.poller

    def schedule(self, path, recursive):
        """Watch path natively (or by polling); returns (observer, watch) or None."""
        if not self.polled(path):
            try:
                return self.observer, self.observer.schedule(self, path, recursive=recursive)
            except OSError as error:
                hint = " (raise fs.inotify.max_user_watches / max_user_instances)" if error.errno in (errno.ENOSPC, errno.EMFILE) else ""
                if self.poll_off:
                    logging.warning("magik_auto_nodes_refresh: cannot watch %s: %s%s; use manual refresh", path, error, hint)
                    return None
                logging.warning("magik_auto_nodes_refresh: cannot watch %s natively: %s%s; polling every %gs instead", path, error, hint, self.poll_interval)
        try:
            poller = self.poller_observer()
            return poller, poller.schedule(self, path, recursive=recursive)
        except OSError as error:
            logging.warning("magik_auto_nodes_refresh: cannot poll %s: %s; use manual refresh", path, error)
            return None

    def signature(self):
        models = folder_paths.models_dir
        models_exist = os.path.isdir(models)
        parts = [(models_exist, None if models_exist else existing_parent(os.path.dirname(models)))]
        for category, watch in self.watches.items():
            for root in watch.roots:
                exists = os.path.isdir(root.path)
                parts.append((
                    category, root.path, exists,
                    None if exists else existing_parent(os.path.dirname(root.path)),
                    frozenset((path, entry.identity) for path, entry in root.directories.items()),
                ))
        return parts

    def tracked(self, path):
        path = os.path.normcase(os.path.abspath(path))
        return path in self.directories or path.startswith(self.scope_prefixes)

    def relevant(self, path):
        path = os.path.normcase(os.path.abspath(path))
        return path.startswith(self.scope_prefixes) or os.path.dirname(path) in self.directories or path in self.interest

    def update_subscriptions(self):
        with self.condition:
            invalidation_pending = bool(self.invalidated)
        signature = self.signature()
        if signature == self.signature_cache and not invalidation_pending and not self.failed:
            return
        directories = {}
        scope = set()
        interest = set()
        for base in (folder_paths.models_dir, os.path.realpath(folder_paths.models_dir)):
            interest.update(ancestors(base))
        required = {(existing_parent(os.path.dirname(folder_paths.models_dir)), False)}
        if os.path.isdir(folder_paths.models_dir):
            required.add((os.path.realpath(folder_paths.models_dir), True))
        for category, watch in self.watches.items():
            for root in watch.roots:
                required.add((existing_parent(os.path.dirname(root.path)), False))
                for base in {os.path.abspath(root.path), os.path.realpath(root.path)}:
                    scope.add(os.path.normcase(base))
                    interest.update(ancestors(base))
                if os.path.isdir(root.path):
                    required.add((os.path.realpath(root.path), root.recursive))
                for path in root.directories:
                    canonical = os.path.normcase(os.path.realpath(path))
                    directories.setdefault(canonical, set()).add((category, root, path))
                    if os.path.islink(path):
                        scope.add(canonical)
                        required.add((os.path.realpath(path), root.recursive))
                        required.add((existing_parent(os.path.dirname(os.path.realpath(path))), False))
        self.directories = directories
        self.interest = interest
        self.scope_prefixes = tuple(base if base.endswith(os.sep) else base + os.sep for base in scope)
        required = {
            (path, recursive) for path, recursive in required
            if not any(other_recursive and other != path and contains(other, path)
                       and self.polled(other) == self.polled(path) for other, other_recursive in required)
            and not (not recursive and (path, True) in required)
        }
        with self.condition:
            invalidated = self.invalidated
            self.invalidated = set()
        expired = {
            key for key in self.subscriptions
            if any(contains(path, key[0]) for path in invalidated)
        }
        for key in (self.subscriptions.keys() - required) | expired:
            observer, watch = self.subscriptions.pop(key)
            try:
                observer.unschedule(watch)
            except Exception:
                logging.debug("magik_auto_nodes_refresh: could not unschedule %s", key[0], exc_info=True)
        self.failed = {key: deadline for key, deadline in self.failed.items() if key in required}
        now = time.monotonic()
        retry_later = {key for key, deadline in self.failed.items() if deadline > now}
        for key in required - self.subscriptions.keys() - retry_later:
            path, recursive = key
            scheduled = self.schedule(path, recursive)
            if scheduled is None:
                self.failed[key] = time.monotonic() + 30.0
                continue
            self.failed.pop(key, None)
            self.subscriptions[key] = scheduled
            if scheduled[0].is_alive() and not self.starting:
                with self.condition:
                    self.pending.update(
                        directory for directory in directories
                        if (recursive and contains(path, directory)) or directory == os.path.normcase(path)
                    )
                    self.last_event = time.monotonic()
                    self.condition.notify()
        self.signature_cache = signature

    def on_any_event(self, event):
        if event.event_type not in ("created", "deleted", "moved") and not (event.is_directory and event.event_type == "modified"):
            return
        paths = {event.src_path}
        if event.event_type == "moved":
            paths.add(event.dest_path)
        paths.discard("")
        if event.event_type == "modified":
            if not self.tracked(event.src_path):
                return
        elif not any(self.relevant(path) for path in paths):
            return
        with self.condition:
            if event.is_directory and event.event_type in ("deleted", "moved"):
                self.invalidated.add(os.path.abspath(event.src_path))
            for path in paths:
                if event.event_type != "modified":
                    self.pending.add(os.path.dirname(os.path.abspath(path)))
                if event.is_directory:
                    self.pending.add(os.path.abspath(path))
            self.last_event = time.monotonic()
            self.condition.notify()

    def affected(self, paths):
        affected = {}
        for path in paths:
            canonical = os.path.normcase(os.path.realpath(path))
            for category, root, logical_path in self.directories.get(canonical, ()):
                affected.setdefault(category, {}).setdefault(root, set()).add(logical_path)
            for category, watch in self.watches.items():
                for root in watch.roots:
                    if contains(path, root.path):
                        affected.setdefault(category, {}).setdefault(root, set()).add(root.path)
        return affected

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.pending or self.resync,
                                        timeout=30.0 if self.failed else None)
                if self.failed:
                    self.resync = True
                if self.stopping:
                    return
                deadline = time.monotonic() + 1
                while not self.stopping:
                    delay = min(self.last_event + 0.2, deadline) - time.monotonic()
                    if delay <= 0:
                        break
                    self.condition.wait(delay)
                if self.stopping:
                    return
                paths = self.pending
                self.pending = set()
                resync = self.resync
                self.resync = False
            try:
                self.process(paths, resync)
            except Exception:
                logging.exception("magik_auto_nodes_refresh: update pass failed; use manual refresh if choices look stale")

    def process(self, paths, resync=False):
        changed = {}
        if resync:
            try:
                for category, changes in self.sync_roots().items():
                    changed[category] = {"added": len(changes["added"]), "removed": len(changes["removed"])}
            except Exception:
                logging.exception("magik_auto_nodes_refresh: could not adopt changed folder paths")
        affected = self.affected(paths)
        for category, directories in affected.items():
            try:
                changes = self.watches[category].refresh(directories)
                if changes is None:
                    continue
                if category in INPUT_CATEGORIES:
                    # Custom nodes list input files by any extension; only skip hidden/partial-download names.
                    if all(ignored_input_name(name) for name in changes["added"] + changes["removed"]):
                        continue
                changed[category] = {"added": len(changes["added"]), "removed": len(changes["removed"])}
            except Exception:
                logging.exception("magik_auto_nodes_refresh: stopped watching %s", category)
                self.watches.pop(category, None)
        if affected or resync:
            try:
                self.update_subscriptions()
            except Exception:
                logging.exception("magik_auto_nodes_refresh: could not update native watches")
        if changed:
            with self.lock:
                self.revision += 1
                event = {
                    "boot_id": self.boot_id,
                    "revision": self.revision,
                    "categories": changed,
                    "nodes": sorted(set().union(*(self.dependencies.get(DEPENDS_ON.get(name, name), ()) for name in changed))),
                }
                self.history.append(event)
            self.server.send_sync("magik_auto_nodes_refresh", event)

    def state(self, since, boot=None):
        with self.lock:
            head = {"boot_id": self.boot_id, "revision": self.revision}
            if since is None:
                return {**head, "events": []}
            # A caller that sends no boot ID (older clients, direct callers) skips the restart check.
            stale_boot = boot is not None and boot != self.boot_id
            if stale_boot or since > self.revision or (self.history and since < self.history[0]["revision"] - 1):
                return {**head, "full_refresh": True}
            return {**head, "events": [event for event in self.history if event["revision"] > since]}

    def record(self, category):
        frame = sys._getframe(1)
        while frame:
            if frame.f_globals.get("__name__") == "server":
                node_class = frame.f_locals.get("node_class")
                if node_class:
                    with self.lock:
                        self.dependencies.setdefault(folder_paths.map_legacy(category), set()).add(node_class)
                    return
            frame = frame.f_back


def install():
    server = PromptServer.instance
    if server is None:
        logging.warning("magik_auto_nodes_refresh: PromptServer is not available; use manual refresh")
        return
    if getattr(folder_paths, "_magik_auto_nodes_refresh", None) is not None:
        return  # already installed (for example, imported twice under different names)
    refresh = FileRefresh(server)

    original_get_filename_list = folder_paths.get_filename_list
    original_get_folder_paths = folder_paths.get_folder_paths
    original_get_input_directory = folder_paths.get_input_directory

    def get_filename_list(category):
        refresh.record(category)
        return original_get_filename_list(category)

    def get_folder_paths(category):
        refresh.record(category)
        return original_get_folder_paths(category)

    def get_input_directory():
        refresh.record("input")
        return original_get_input_directory()

    folder_paths.get_filename_list = get_filename_list
    folder_paths.get_folder_paths = get_folder_paths
    folder_paths.get_input_directory = get_input_directory

    original_add_model_folder_path = folder_paths.add_model_folder_path

    def add_model_folder_path(*args, **kwargs):
        result = original_add_model_folder_path(*args, **kwargs)
        refresh.request_resync()
        return result

    folder_paths.add_model_folder_path = add_model_folder_path

    @server.routes.get("/magik_auto_nodes_refresh/state")
    async def get_state(request):
        raw = request.query.get("since", "0")
        if raw == "head":
            return web.json_response(refresh.state(None))
        try:
            since = max(0, int(raw))
        except ValueError:
            return web.Response(status=400)
        return web.json_response(refresh.state(since, request.query.get("boot")))

    async def on_startup(app):
        try:
            refresh.start()
        except Exception as error:
            logging.exception("magik_auto_nodes_refresh: native watcher unavailable: %s; use manual refresh", error)
            try:
                refresh.stop()
            except Exception:
                logging.exception("magik_auto_nodes_refresh: cleanup after failed start also failed")

    async def on_cleanup(app):
        refresh.stop()

    async def on_response(request, response):
        if request.path != "/object_info" or response.status != 200:
            return
        # A full definitions load is a good moment to notice folder lists changed behind our back.
        refresh.request_resync()
        if not refresh.dependencies:
            warn_once(
                "dependencies",
                "magik_auto_nodes_refresh: /object_info completed but no node dependencies were recorded; "
                "ComfyUI's server internals may have changed, so use manual refresh",
            )

    server.app.on_startup.append(on_startup)
    server.app.on_cleanup.append(on_cleanup)
    server.app.on_response_prepare.append(on_response)
    folder_paths._magik_auto_nodes_refresh = refresh
