#!/usr/bin/env python3
"""Kali Launcher: browse and run Kali tools (Docker containers or host) in terminal tabs."""
import datetime
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte

HOST = "Host (this machine)"
DATA_DIR = os.path.expanduser("~/kali-data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
STATE_FILE = os.path.expanduser("~/.config/kali-launcher/state.json")
MAX_RECENTS = 10
FALLBACK_MIN = 30

# Runs inside the source (docker exec -i ... python3 -) and prints JSON.
LIST_SCRIPT = r'''
import glob, json, os
seen, tools = set(), []
dirs = ["/usr/share/kali-menu/applications", "/usr/share/applications"]
for d in dirs:
    for path in sorted(glob.glob(d + "/*.desktop")):
        try:
            entry, inside = {}, False
            for line in open(path, encoding="utf-8", errors="replace"):
                line = line.strip()
                if line.startswith("["):
                    inside = line == "[Desktop Entry]"
                elif inside and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    entry.setdefault(k.strip(), v.strip())
            if entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true":
                continue
            name, cmd = entry.get("Name", ""), entry.get("Exec", "")
            if not name or not cmd or name in seen:
                continue
            cats = [c for c in entry.get("Categories", "").split(";") if c]
            kali = [c for c in cats if c.startswith("kali-")]
            cat = (kali or cats or ["Other"])[0]
            seen.add(name)
            tools.append({"name": name, "desc": entry.get("Comment", ""), "exec": cmd, "cat": cat})
        except Exception:
            pass
def binary(t):
    first = t["exec"].split()[0] if t["exec"].split() else ""
    return os.path.basename(first)
import re, subprocess
blank = [t for t in tools if not t["desc"]]
if blank:
    names = sorted({binary(t) for t in blank} | {t["name"].lower().replace(" ", "-") for t in blank})
    try:
        out = subprocess.run(["whatis"] + names, capture_output=True, text=True, timeout=60).stdout
        found = {}
        for line in out.splitlines():
            left, sep, right = line.partition(" - ")
            if sep:
                for n in re.findall(r"([^\s,]+?)\s*\(\d[^)]*\)", left):
                    found.setdefault(n.lower(), right.strip())
        for t in blank:
            t["desc"] = found.get(binary(t).lower()) or found.get(t["name"].lower().replace(" ", "-"), "")
    except Exception:
        pass
if len(tools) < 30:
    tools = []
    for d in ("/usr/bin", "/usr/sbin"):
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                p = os.path.join(d, n)
                if n not in seen and os.path.isfile(p) and os.access(p, os.X_OK):
                    seen.add(n)
                    tools.append({"name": n, "desc": "", "exec": n, "cat": "All Commands"})
print(json.dumps(tools))
'''


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw)


def find_sources():
    sources = []
    try:
        out = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}"]).stdout
        for line in out.splitlines():
            name, _, image = line.partition("\t")
            if "kali" in name.lower() or "kali" in image.lower():
                sources.append(name)
    except Exception:
        pass
    return sources + [HOST]


def ensure_running(source):
    if source == HOST:
        return
    state = run(["docker", "inspect", "-f", "{{.State.Running}}", source])
    if state.returncode != 0:
        raise RuntimeError(state.stderr.strip() or f"cannot inspect {source}")
    if state.stdout.strip() != "true":
        started = run(["docker", "start", source], timeout=60)
        if started.returncode != 0:
            raise RuntimeError(started.stderr.strip() or f"cannot start {source}")


def list_tools(source):
    ensure_running(source)
    base = ["python3", "-"] if source == HOST else ["docker", "exec", "-i", source, "python3", "-"]
    res = run(base, input=LIST_SCRIPT, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "tool listing failed")
    return json.loads(res.stdout)


def clean_exec(cmd):
    return re.sub(r"\s+", " ", re.sub(r"%[a-zA-Z]", "", cmd.replace("%%", "\0"))).replace("\0", "%").strip()


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        return {"favorites": list(st.get("favorites", [])), "recents": list(st.get("recents", [])),
                "target": str(st.get("target", "")), "scope": list(st.get("scope", []))}
    except Exception:
        return {"favorites": [], "recents": [], "target": "", "scope": []}


TARGET_RE = re.compile(r"^[A-Za-z0-9._:/*-]+$")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")


def in_scope(host, scope):
    """True if host (IP, CIDR or domain) is covered by an entry in the scope list."""
    host = host.strip().lower()
    try:
        net = ipaddress.ip_network(host, strict=False)
    except ValueError:
        net = None
    for entry in scope:
        entry = entry.strip().lower()
        if not entry:
            continue
        if net is not None:
            try:
                allowed = ipaddress.ip_network(entry, strict=False)
                if net.version == allowed.version and net.subnet_of(allowed):
                    return True
            except ValueError:
                pass
            continue
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def out_of_scope_hosts(cmd, target, scope):
    """Hosts a command would touch that are not covered by the scope list."""
    found = {m for m in IPV4_RE.findall(cmd) if not m.startswith(("127.", "0."))}
    if target:
        found.add(target)
    return sorted(h for h in found if not in_scope(h, scope))


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1)
    except Exception:
        pass


def has_x11_mount(source):
    if source == HOST:
        return True
    try:
        out = run(["docker", "inspect", "-f", "{{range .Mounts}}{{.Destination}} {{end}}", source]).stdout
        return "/tmp/.X11-unix" in out.split()
    except Exception:
        return False


def allow_x11():
    """Let root in the container talk to the host display (best effort)."""
    if shutil.which("xhost") and os.environ.get("DISPLAY"):
        try:
            run(["xhost", "+SI:localuser:root"], timeout=5)
        except Exception:
            pass


def gui_setup_script(container):
    img = "kali-gui-backup"
    return (
        f"echo 'This recreates container {container} with the X11 socket mounted.'; "
        f"echo 'Installed tools are kept: the container is committed to image {img} first.'; "
        f"read -p 'Type yes to continue: ' a; [ \"$a\" = yes ] || exit 0; "
        f"mkdir -p {shlex.quote(DATA_DIR)} && "
        f"docker commit {container} {img} && docker stop {container} && docker rm {container} && "
        f"docker run -dit --name {container} --network host "
        f"-e DISPLAY=\"$DISPLAY\" -v /tmp/.X11-unix:/tmp/.X11-unix -v {shlex.quote(DATA_DIR)}:/data "
        f"{img} sleep infinity && echo 'Done. GUI apps should now open as windows.'"
    )


def shell_argv(source, cmd):
    wrapped = f"{cmd}; echo; echo '[command finished]'; exec bash"
    if source == HOST:
        return ["bash", "-lc", wrapped]
    display = os.environ.get("DISPLAY", ":0")
    return ["docker", "exec", "-it", "-e", "TERM=xterm-256color", "-e", f"DISPLAY={display}",
            source, "bash", "-lc", wrapped]


def log_argv(argv, source, title):
    """Wrap argv in `script` so the whole session is logged to ~/kali-data/logs."""
    if not shutil.which("script"):
        return argv
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        return argv
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{source}-{title}")[:60]
    return ["script", "-qf", os.path.join(LOG_DIR, f"{stamp}-{safe}.log"), "-c", shlex.join(argv)]


USAGE_SH = r'''
for c in "$@"; do
  if command -v "$c" >/dev/null 2>&1; then
    if man -w "$c" >/dev/null 2>&1; then MANPAGER=cat man -P cat "$c" 2>/dev/null | col -b | head -300; exit 0; fi
    for f in --help -h help; do
      out=$(timeout 8 "$c" $f </dev/null 2>&1 | head -300)
      if [ -n "$out" ]; then echo "$out"; exit 0; fi
    done
  fi
done
echo "No man page or --help output found. Run the tool in a tab and try its own help."
'''


def candidates(name, cmd):
    first = cmd.split()[0].rsplit("/", 1)[-1] if cmd.split() else ""
    return [c for c in dict.fromkeys([first, name.lower().replace(" ", "-")]) if c]


def fetch_usage(source, name, cmd):
    ensure_running(source)
    argv = ["bash", "-c", USAGE_SH, "usage"] + candidates(name, cmd)
    if source != HOST:
        argv = ["docker", "exec", "-i", source] + argv
    res = run(argv, timeout=40)
    return (res.stdout or res.stderr).strip() or "No usage text available."


class Launcher(Gtk.Window):
    def __init__(self):
        super().__init__(title="Kali Launcher")
        self.set_default_size(1280, 780)
        self.connect("destroy", Gtk.main_quit)
        self.state = load_state()
        self.tools = []
        self.source = HOST
        self.loading = 0

        header = Gtk.HeaderBar(show_close_button=True, title="Kali Launcher")
        self.set_titlebar(header)
        new_tab = Gtk.Button(label="+")
        new_tab.set_tooltip_text("New shell tab")
        new_tab.connect("clicked", lambda *_: self.open_tab(self.source, "bash", "shell"))
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Reload tool list")
        refresh.connect("clicked", lambda *_: self.reload())
        logs = Gtk.Button.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON)
        logs.set_tooltip_text("Open session logs folder")
        logs.connect("clicked", self.open_logs)
        gui = Gtk.Button(label="GUI setup")
        gui.set_tooltip_text("Enable graphical apps (Burp, Wireshark...) in this container")
        gui.connect("clicked", self.on_gui_setup)
        header.pack_end(logs)
        header.pack_end(gui)
        header.pack_start(new_tab)
        header.pack_start(refresh)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(paned)

        # Left: source, search, tool tree, status
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        left.set_size_request(380, -1)
        self.combo = Gtk.ComboBoxText()
        self.combo.connect("changed", self.on_source_changed)
        self.search = Gtk.SearchEntry(placeholder_text="Search tools...")
        self.search.connect("search-changed", lambda *_: self.populate())
        self.store = Gtk.TreeStore(str, str, bool, str, str, str, str)  # markup, exec, is_tool, name, desc, info icon, star icon
        self.tree = Gtk.TreeView(model=self.store, headers_visible=False)
        self.tree.append_column(Gtk.TreeViewColumn("Tool", Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END), markup=0))
        info_col = Gtk.TreeViewColumn("Info", Gtk.CellRendererPixbuf(), icon_name=5)
        star_col = Gtk.TreeViewColumn("Fav", Gtk.CellRendererPixbuf(), icon_name=6)
        self.tree.append_column(star_col)
        self.star_col = star_col
        self.tree.append_column(info_col)
        self.info_col = info_col
        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.connect("button-press-event", self.on_tree_click)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(self.tree)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        left.pack_start(self.combo, False, False, 0)
        left.pack_start(self.search, False, False, 0)
        left.pack_start(scroll, True, True, 0)
        left.pack_start(self.status, False, False, 0)
        paned.pack1(left, False, False)

        # Right: command box + terminal tabs
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        trow = Gtk.Box(spacing=6)
        trow.pack_start(Gtk.Label(label="Target"), False, False, 0)
        self.target_entry = Gtk.Entry(placeholder_text="IP, CIDR or domain; use {target} in commands",
                                      text=self.state["target"], hexpand=True)
        self.target_entry.connect("changed", self.on_target_changed)
        scope_btn = Gtk.Button(label="Scope...")
        scope_btn.set_tooltip_text("Edit the list of authorized targets")
        scope_btn.connect("clicked", self.edit_scope)
        self.scope_label = Gtk.Label(use_markup=True)
        trow.pack_start(self.target_entry, True, True, 0)
        trow.pack_start(self.scope_label, False, False, 0)
        trow.pack_start(scope_btn, False, False, 0)
        right.pack_start(trow, False, False, 0)
        self.update_scope_label()
        self.cmd_entry = Gtk.Entry(placeholder_text="Run any command (Enter opens a new tab)...")
        self.cmd_entry.connect("activate", self.on_cmd_activate)
        self.notebook = Gtk.Notebook(scrollable=True)
        right.pack_start(self.cmd_entry, False, False, 0)
        right.pack_start(self.notebook, True, True, 0)
        paned.pack2(right, True, False)

        self.sources = find_sources()
        for s in self.sources:
            self.combo.append_text(s)
        self.combo.set_active(0)  # triggers first load
        self.open_tab(self.source, "bash", "shell")
        self.show_all()

    # ---- listing ----
    def reload(self):
        self.loading += 1
        token = self.loading
        source = self.source
        self.tools = []
        self.store.clear()
        self.status.set_text(f"Loading tools from {source}...")

        def work():
            try:
                tools = list_tools(source)
                GLib.idle_add(self.loaded, token, source, tools, None)
            except Exception as e:  # never crash on listing
                GLib.idle_add(self.loaded, token, source, [], str(e))

        threading.Thread(target=work, daemon=True).start()

    def loaded(self, token, source, tools, error):
        if token != self.loading:
            return
        self.tools = tools
        if error:
            self.status.set_text(f"Error: {error}")
            return
        self.populate()

    def populate(self):
        q = self.search.get_text().strip().lower()
        self.store.clear()
        cats = {}
        shown = 0
        for t in self.tools:
            if q and q not in f"{t['name']} {t['desc']} {t['cat']}".lower():
                continue
            cats.setdefault(t["cat"], []).append(t)
            shown += 1
        by_name = {t["name"]: t for t in self.tools}
        pinned = []
        if not q:
            favs = [by_name[n] for n in self.state["favorites"] if n in by_name]
            recs = [by_name[n] for n in self.state["recents"] if n in by_name]
            if favs:
                pinned.append(("\u2605 Favorites", favs))
            if recs:
                pinned.append(("Recent", recs))
        for label, group in pinned:
            parent = self.store.append(None, [f"<b>{label}</b>  ({len(group)})", "", False, "", "", "", ""])
            for t in group:
                self.add_tool_row(parent, t)
            self.tree.expand_row(self.store.get_path(parent), False)
        for cat in sorted(cats):
            label = cat.replace("kali-", "").replace("-", " ").title()
            parent = self.store.append(None, [f"<b>{GLib.markup_escape_text(label)}</b>  ({len(cats[cat])})", "", False, "", "", "", ""])
            for t in sorted(cats[cat], key=lambda x: x["name"].lower()):
                self.add_tool_row(parent, t)
        if q:
            self.tree.expand_all()
        self.status.set_text(f"{shown} of {len(self.tools)} tools · {self.source}")

    def add_tool_row(self, parent, t):
        desc = f"\n<small>{GLib.markup_escape_text(t['desc'])}</small>" if t["desc"] else ""
        star = "starred-symbolic" if t["name"] in self.state["favorites"] else "non-starred-symbolic"
        self.store.append(parent, [f"{GLib.markup_escape_text(t['name'])}{desc}", t["exec"], True,
                                   t["name"], t["desc"], "dialog-information-symbolic", star])

    # ---- events ----
    def on_source_changed(self, combo):
        text = combo.get_active_text()
        if text:
            self.source = text
            self.reload()

    def on_row_activated(self, tree, path, _col):
        row = self.store[path]
        if row[2]:
            self.open_tab(self.source, clean_exec(row[1]), row[3], track=True)
        elif tree.row_expanded(path):
            tree.collapse_row(path)
        else:
            tree.expand_row(path, False)

    def on_tree_click(self, tree, event):
        if event.button != 1:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit and hit[1] is self.star_col and self.store[hit[0]][2]:
            name = self.store[hit[0]][3]
            favs = self.state["favorites"]
            favs.remove(name) if name in favs else favs.append(name)
            save_state(self.state)
            self.populate()
            return True
        if hit and hit[1] is self.info_col and self.store[hit[0]][2]:
            row = self.store[hit[0]]
            self.show_info(row[3], row[4], clean_exec(row[1]))
            return True
        return False

    def show_info(self, name, desc, cmd):
        dlg = Gtk.Dialog(title=name, transient_for=self, modal=False)
        dlg.set_default_size(760, 560)
        box = dlg.get_content_area()
        box.set_spacing(6)
        box.set_border_width(10)
        title = Gtk.Label(xalign=0, use_markup=True)
        title.set_markup(f"<big><b>{GLib.markup_escape_text(name)}</b></big>")
        about = Gtk.Label(label=desc or "No short description available.", xalign=0, wrap=True, selectable=True)
        command = Gtk.Label(xalign=0, use_markup=True, selectable=True)
        command.set_markup(f"<b>Command:</b> <tt>{GLib.markup_escape_text(cmd)}</tt>")
        buf = Gtk.TextBuffer()
        buf.set_text("Loading usage...")
        view = Gtk.TextView(buffer=buf, editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            left_margin=6, right_margin=6)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(view)
        for w in (title, about, command):
            box.pack_start(w, False, False, 0)
        box.pack_start(Gtk.Label(label="How to use it", xalign=0), False, False, 0)
        box.pack_start(scroll, True, True, 0)
        run_btn = dlg.add_button("Run in new tab", Gtk.ResponseType.OK)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        source = self.source

        def on_response(d, resp):
            if resp == Gtk.ResponseType.OK:
                self.open_tab(source, cmd, name, track=True)
            d.destroy()

        dlg.connect("response", on_response)
        dlg.show_all()

        def work():
            try:
                text = fetch_usage(source, name, cmd)
            except Exception as e:
                text = f"Error: {e}"
            GLib.idle_add(buf.set_text, text)

        threading.Thread(target=work, daemon=True).start()

    def update_scope_label(self):
        t = self.state["target"]
        if not t:
            text = ""
        elif not self.state["scope"]:
            text = "<span foreground='#d9a400'>no scope list</span>"
        elif in_scope(t, self.state["scope"]):
            text = "<span foreground='#2e9d4f'>in scope</span>"
        else:
            text = "<span foreground='#d33'><b>NOT in scope</b></span>"
        self.scope_label.set_markup(text)

    def on_target_changed(self, entry):
        self.state["target"] = entry.get_text().strip()
        save_state(self.state)
        self.update_scope_label()

    def edit_scope(self, *_):
        dlg = Gtk.Dialog(title="Authorized scope", transient_for=self, modal=True)
        dlg.set_default_size(460, 360)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.set_spacing(6)
        box.pack_start(Gtk.Label(
            label="One per line: IP, CIDR (10.0.0.0/24), domain (example.com covers its subdomains) "
                  "or *.example.com. Only test systems you own or have written permission to test.",
            xalign=0, wrap=True), False, False, 0)
        buf = Gtk.TextBuffer()
        buf.set_text("\n".join(self.state["scope"]))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(Gtk.TextView(buffer=buf, monospace=True))
        box.pack_start(scroll, True, True, 0)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Save", Gtk.ResponseType.OK)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            self.state["scope"] = [ln.strip() for ln in text.splitlines() if ln.strip()]
            save_state(self.state)
            self.update_scope_label()
        dlg.destroy()

    def confirm_out_of_scope(self, hosts):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE, text="Target not on your authorized list")
        dlg.format_secondary_text(", ".join(hosts) + "\n\nOnly continue if you have permission to test it.")
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Run anyway", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        return ok

    def resolve_command(self, cmd):
        """Fill {target}, then run the scope check. Returns the final command or None to abort."""
        target = self.state["target"]
        uses_target = "{target}" in cmd
        if uses_target:
            if not target:
                self.status.set_text("This command uses {target}: set a Target first.")
                return None
            if not TARGET_RE.match(target):
                self.status.set_text("Target has unsupported characters.")
                return None
            cmd = cmd.replace("{target}", shlex.quote(target))
        scope = self.state["scope"]
        if uses_target or scope:
            bad = out_of_scope_hosts(cmd, target if uses_target else "", scope)
            if bad and not self.confirm_out_of_scope(bad):
                self.status.set_text("Cancelled: " + ", ".join(bad) + " not in scope.")
                return None
        return cmd

    def open_logs(self, *_):
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", LOG_DIR])
        except Exception as e:
            self.status.set_text(f"Error: {e}")

    def on_gui_setup(self, *_):
        if self.source == HOST:
            self.status.set_text("Host apps already use your display; pick a container for GUI setup.")
            return
        if has_x11_mount(self.source):
            self.status.set_text(f"{self.source} already has the X11 socket mounted.")
            return
        self.open_tab(HOST, gui_setup_script(self.source), "gui-setup")

    def on_cmd_activate(self, entry):
        cmd = entry.get_text().strip()
        if cmd:
            self.open_tab(self.source, cmd, cmd.split()[0])
            entry.set_text("")

    # ---- terminal tabs ----
    def open_tab(self, source, cmd, title, track=False):
        if title != "gui-setup":
            cmd = self.resolve_command(cmd)
            if cmd is None:
                return
        try:
            ensure_running(source)
        except Exception as e:
            self.status.set_text(f"Error: {e}")
            return
        if source != HOST:
            allow_x11()
        if track:
            rec = self.state["recents"]
            if title in rec:
                rec.remove(title)
            rec.insert(0, title)
            del rec[MAX_RECENTS:]
            save_state(self.state)
            self.populate()
        term = Vte.Terminal(scrollback_lines=20000)
        term.set_font(Pango.FontDescription("Monospace 11"))
        term.connect("key-press-event", self.on_term_key)
        term.spawn_async(Vte.PtyFlags.DEFAULT, None, log_argv(shell_argv(source, cmd), source, title), None,
                         GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, None)

        label = Gtk.Box(spacing=4)
        label.pack_start(Gtk.Label(label=title), False, False, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        label.pack_start(close, False, False, 0)
        label.show_all()

        page = self.notebook.append_page(term, label)
        self.notebook.set_tab_reorderable(term, True)
        term.show()
        self.notebook.set_current_page(page)
        close.connect("clicked", lambda *_: self.close_tab(term))
        term.connect("child-exited", lambda *_: self.close_tab(term))
        term.grab_focus()

    def close_tab(self, term):
        idx = self.notebook.page_num(term)
        if idx >= 0:
            self.notebook.remove_page(idx)

    def on_term_key(self, term, event):
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if (event.state & ctrl_shift) == ctrl_shift:
            if event.keyval in (Gdk.KEY_C, Gdk.KEY_c):
                term.copy_clipboard_format(Vte.Format.TEXT)
                return True
            if event.keyval in (Gdk.KEY_V, Gdk.KEY_v):
                term.paste_clipboard()
                return True
        return False


if __name__ == "__main__":
    Launcher()
    Gtk.main()
