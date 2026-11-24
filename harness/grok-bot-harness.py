#!/usr/bin/env python3
"""Run Grok Bot under a watchdog that records what it and GNOME Shell do.

Each run gets its own directory (default ~/grok-bot-harness/<date-time>/):

  metrics.tsv   every second: system memory, GNOME Shell and Grok Bot
                memory, lock and display state, Wayland traffic totals
  wayland.tsv   every 2 s: how often each Wayland message was sent or received
  drm.tsv       every 5 s: GPU memory GNOME Shell holds, if the driver says
  journal.log   kernel and GNOME Shell log lines during the run
  events.log    start, trip, kill, exit
  trip/         snapshot taken when the watchdog trips
  grok-bot.log  Grok Bot's own output, without the Wayland trace
  report.md     summary to share

The watchdog trips when GNOME Shell's shared memory rises fast or free memory
runs low. It then stops Grok Bot, so the desktop survives, and keeps
measuring for a while to see whether GNOME Shell gives the memory back.

`--report DIR` rebuilds report.md from a run that ended abruptly, for
example when the whole session crashed.
"""

import argparse
import collections
import csv
import datetime
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

PAGE = os.sysconf("SC_PAGE_SIZE")
MB = 1024 * 1024

METRIC_FIELDS = [
    "time", "mem_avail_mb", "anon_mb", "shmem_mb", "unevictable_mb",
    "gs_pid", "gs_cg_shmem_mb", "gs_cg_mb", "gs_vmsize_mb", "gs_rss_mb",
    "gb_cg_shmem_mb", "gb_cg_mb", "gb_rss_mb", "gb_procs",
    "locked", "power_save", "wl_msgs", "wl_commits", "wl_attaches",
]

# libwayland's WAYLAND_DEBUG lines, for example
#   [1234567.123] {Default Queue}  -> wl_surface#45.commit()
#   [1234567.130] {Default Queue} wl_buffer#60.release()
# "->" marks a request sent to the compositor; no arrow marks an event.
WAYLAND_LINE = re.compile(
    r"^\[\s*\d+\.\d+\]\s*(?:\{[^}]*\}\s*)?(?:discarded\s+)?(->\s*)?"
    r"([A-Za-z_]\w*)[#@]\d+\.(\w+)\("
)

JOURNAL_KEEP = re.compile(
    r"oom|out of memory|killed process|segfault|SEGV|dumped core|gnome-shell|"
    r"mutter|grok-bot|appledrm|apple-dcp|flip_done|commit wait|BO creation|"
    r"VM_BIND|drm",
    re.IGNORECASE,
)

GNOME_SHELL_COMMS = (".gnome-shell-wr", "gnome-shell")


def clock():
    return datetime.datetime.now().strftime("%H:%M:%S")


def read(path, default=""):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def mb(value):
    return "" if value is None else str(round(value / MB))


def meminfo():
    out = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) * 1024
    return out


def processes():
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            comm = read(f"/proc/{entry}/comm").strip()
            if comm:
                yield int(entry), comm


def cgroup_of(pid):
    for line in read(f"/proc/{pid}/cgroup").splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup" + line[3:])
    return None


def cgroup_memory(cg):
    """Return (shmem, current) in bytes for a cgroup, or (None, None)."""
    if cg is None:
        return None, None
    shmem = None
    for line in read(cg / "memory.stat").splitlines():
        key, _, value = line.partition(" ")
        if key == "shmem":
            shmem = int(value)
            break
    current = read(cg / "memory.current").strip()
    return shmem, int(current) if current.isdigit() else None


def find_gnome_shell():
    fallback = None
    for pid, comm in processes():
        if comm not in GNOME_SHELL_COMMS:
            continue
        cg = cgroup_of(pid)
        if cg is not None and "org.gnome.Shell@" in str(cg):
            return pid
        fallback = fallback or pid
    return fallback


def status_field(pid, field):
    for line in read(f"/proc/{pid}/status").splitlines():
        key, _, rest = line.partition(":")
        if key == field:
            return int(rest.split()[0]) * 1024
    return None


def rss(pid):
    fields = read(f"/proc/{pid}/statm").split()
    return int(fields[1]) * PAGE if len(fields) > 1 else 0


def gdbus(*args):
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session", *args],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def screen_state():
    """Return (locked, power_save) as strings; empty when unknown.

    power_save is Mutter's PowerSaveMode: 0 on, 1 standby, 2 suspend, 3 off.
    """
    locked = gdbus(
        "--dest", "org.gnome.ScreenSaver",
        "--object-path", "/org/gnome/ScreenSaver",
        "--method", "org.gnome.ScreenSaver.GetActive",
    )
    power = gdbus(
        "--dest", "org.gnome.Mutter.DisplayConfig",
        "--object-path", "/org/gnome/Mutter/DisplayConfig",
        "--method", "org.freedesktop.DBus.Properties.Get",
        "org.gnome.Mutter.DisplayConfig", "PowerSaveMode",
    )
    locked_s = "" if locked is None else ("1" if "true" in locked else "0")
    match = re.search(r"-?\d+", power or "")
    return locked_s, match.group(0) if match else ""


def drm_usage(pid):
    """Sum the drm-* memory keys over the DRM clients a process has open."""
    units = {"": 1, "B": 1, "KiB": 1024, "MiB": MB, "GiB": 1024 * MB}
    try:
        fds = os.listdir(f"/proc/{pid}/fdinfo")
    except OSError:
        return {}
    clients = {}
    for fd in fds:
        text = read(f"/proc/{pid}/fdinfo/{fd}")
        if "drm-client-id" not in text:
            continue
        info, client = {}, None
        for line in text.splitlines():
            key, _, value = line.partition(":")
            value = value.strip()
            if key in ("drm-client-id", "drm-driver"):
                client = (client or "") + f"{key}={value};"
            elif key.startswith(("drm-total-", "drm-resident-", "drm-shared-",
                                 "drm-active-", "drm-purgeable-")):
                parts = value.split()
                try:
                    number = float(parts[0])
                except (IndexError, ValueError):
                    continue
                unit = parts[1] if len(parts) > 1 else ""
                info[key] = number * units.get(unit, 1)
        if client is not None:
            clients[client] = info
    totals = collections.Counter()
    for info in clients.values():
        totals.update(info)
    return totals


class TextSink:
    """Line-buffered log file with a size cap."""

    def __init__(self, path, cap_bytes):
        self.file = open(path, "w", buffering=1, errors="replace")
        self.left = cap_bytes
        self.lock = threading.Lock()

    def write(self, line):
        with self.lock:
            if self.left <= 0:
                return
            self.left -= len(line) + 1
            self.file.write(line + "\n")
            if self.left <= 0:
                self.file.write("[log capped]\n")


class Events:
    def __init__(self, path):
        self.file = open(path, "a", buffering=1)

    def add(self, kind, text=""):
        line = f"{clock()} {kind} {text}".rstrip()
        self.file.write(line + "\n")
        print(f"[harness] {line}", flush=True)


class WaylandTap:
    """Counts Wayland messages in Grok Bot's stderr and keeps the rest."""

    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.window = collections.Counter()
        self.tail = collections.deque(maxlen=4000)

    def feed(self, stream):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            match = WAYLAND_LINE.match(line)
            if match is None:
                self.log.write(line)
                continue
            key = ("> " if match.group(1) else "< ") + f"{match.group(2)}.{match.group(3)}"
            with self.lock:
                self.window[key] += 1
                self.tail.append(line)

    def take(self):
        with self.lock:
            counts, self.window = self.window, collections.Counter()
        return counts

    def tail_lines(self):
        with self.lock:
            return list(self.tail)


def follow_journal(sink, stop):
    try:
        proc = subprocess.Popen(
            ["journalctl", "-f", "-n", "0", "-o", "short-precise"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            errors="replace",
        )
    except OSError:
        sink.write("journalctl not available")
        return None
    def run():
        for line in proc.stdout:
            if stop.is_set():
                break
            if JOURNAL_KEEP.search(line):
                sink.write(line.rstrip("\n"))
    threading.Thread(target=run, daemon=True).start()
    return proc


def grok_bot_pids(cg):
    if cg is not None:
        procs = read(cg / "cgroup.procs").split()
        if procs:
            return [int(p) for p in procs]
    return [pid for pid, comm in processes() if comm == "grok-bot"]


def write_trip_snapshot(out, gs_pid, gb_pids, tap, history):
    trip = out / "trip"
    trip.mkdir(exist_ok=True)
    (trip / "meminfo.txt").write_text(read("/proc/meminfo"))
    if gs_pid:
        (trip / "gnome-shell-status.txt").write_text(read(f"/proc/{gs_pid}/status"))
        (trip / "gnome-shell-smaps_rollup.txt").write_text(read(f"/proc/{gs_pid}/smaps_rollup"))
        # Group GNOME Shell's mappings by what they map: many mappings of a
        # DRM node point at GPU buffers.
        groups = collections.defaultdict(lambda: [0, 0])
        for line in read(f"/proc/{gs_pid}/maps").splitlines():
            fields = line.split(maxsplit=5)
            start, _, end = fields[0].partition("-")
            name = fields[5] if len(fields) > 5 else "[anonymous]"
            groups[name][0] += 1
            groups[name][1] += int(end, 16) - int(start, 16)
        rows = sorted(groups.items(), key=lambda item: -item[1][1])[:40]
        (trip / "gnome-shell-maps-summary.txt").write_text(
            "count\tsize_mb\tmapping\n"
            + "".join(f"{c}\t{round(s / MB)}\t{n}\n" for n, (c, s) in rows)
        )
        drm_lines = []
        for fd in sorted(os.listdir(f"/proc/{gs_pid}/fdinfo"), key=lambda f: int(f) if f.isdigit() else 0):
            text = read(f"/proc/{gs_pid}/fdinfo/{fd}")
            if "drm-" in text:
                drm_lines.append(f"## fd {fd}\n" + "".join(
                    l + "\n" for l in text.splitlines() if l.startswith("drm-")))
        (trip / "gnome-shell-drm-fdinfo.txt").write_text("".join(drm_lines))
    big = sorted(((rss(pid), pid, comm) for pid, comm in processes()), reverse=True)[:40]
    (trip / "top-processes.txt").write_text(
        "rss_mb\tpid\tcomm\n" + "".join(f"{round(r / MB)}\t{p}\t{c}\n" for r, p, c in big))
    (trip / "grok-bot-pids.txt").write_text("".join(f"{p}\n" for p in gb_pids))
    total = collections.Counter()
    for _, counts in history:
        total.update(counts)
    seconds = max(len(history), 1)
    (trip / "wayland-last-60s.txt").write_text(
        f"per second, averaged over the last {seconds} s\n"
        + "".join(f"{n / seconds:8.1f}  {k}\n" for k, n in total.most_common(40)))
    (trip / "wayland-tail.log").write_text("\n".join(tap.tail_lines()) + "\n")
    lines = []
    for stat in Path("/sys/fs/cgroup/user.slice").rglob("memory.stat"):
        for line in read(stat).splitlines():
            if line.startswith("shmem "):
                value = int(line.split()[1])
                if value > 200 * MB:
                    lines.append((value, str(stat.parent)))
                break
    lines.sort(reverse=True)
    (trip / "cgroups-by-shmem.txt").write_text(
        "".join(f"{round(v / MB)}\t{p}\n" for v, p in lines[:30]))


def stop_grok_bot(pids_fn, events):
    pids = pids_fn()
    events.add("KILL", f"SIGTERM to {len(pids)} Grok Bot processes")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and pids_fn():
        time.sleep(0.2)
    left = pids_fn()
    if left:
        events.add("KILL", f"SIGKILL to {len(left)} processes still running")
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def run(args):
    out = Path(args.out).expanduser() if args.out else (
        Path.home() / "grok-bot-harness" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)

    already = [pid for pid, comm in processes() if comm == "grok-bot"]
    if already:
        print("Grok Bot is already running (pids "
              + " ".join(map(str, already)) + ").\n"
              "A new launch would hand off to it and exit, so quit it first:\n"
              "  pkill grok-bot", file=sys.stderr)
        return 1

    events = Events(out / "events.log")
    gnome_version = ""
    try:
        gnome_version = subprocess.run(["gnome-shell", "--version"], capture_output=True,
                                       text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    events.add("START", f"dir={out}")
    events.add("INFO", f"grok-bot={args.grok_bot} args={' '.join(args.grok_args) or '-'}")
    events.add("INFO", f"kernel={os.uname().release} machine={os.uname().machine} {gnome_version}")
    events.add("INFO", f"trip: GNOME Shell shmem +{args.trip_rise} MB in {args.trip_window} s, "
                       f"or MemAvailable < {args.trip_avail} MB; kill={'no' if args.no_kill else 'yes'}")
    print(f"[harness] If the session crashes, log back in and run:\n"
          f"  nix run github:portal-wheatley/Grok-Bot-for-Linux#harness -- --report {out}",
          flush=True)

    env = dict(os.environ)
    if not args.no_wayland_debug:
        env["WAYLAND_DEBUG"] = "1"
    command = [args.grok_bot, *args.grok_args]
    unit = f"grok-bot-harness-{out.name}"
    if shutil.which("systemd-run") and subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "true"],
            capture_output=True).returncode == 0:
        command = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}", "--", *command]
    else:
        events.add("INFO", "systemd-run --user unavailable; Grok Bot runs without its own cgroup")

    stdout_log = open(out / "grok-bot.stdout.log", "w")
    child = subprocess.Popen(command, stdout=stdout_log, stderr=subprocess.PIPE, env=env)
    events.add("LAUNCH", f"pid={child.pid}")

    tap = WaylandTap(TextSink(out / "grok-bot.log", 50 * MB))
    threading.Thread(target=tap.feed, args=(child.stderr,), daemon=True).start()
    stop = threading.Event()
    journal = follow_journal(TextSink(out / "journal.log", 20 * MB), stop)

    interrupted = []
    def on_signal(signum, _frame):
        interrupted.append(signal.Signals(signum).name)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, on_signal)

    metrics = open(out / "metrics.tsv", "w", buffering=1, newline="")
    writer = csv.writer(metrics, delimiter="\t", lineterminator="\n")
    writer.writerow(METRIC_FIELDS)
    wayland = open(out / "wayland.tsv", "w", buffering=1)
    wayland.write("time\tmessage\tcount\n")
    drm = open(out / "drm.tsv", "w", buffering=1)
    drm.write("time\tkey\tmb\n")

    gb_cg = None
    gs_pid = None
    history = collections.deque(maxlen=60)
    window = collections.Counter()
    rises = collections.deque(maxlen=args.trip_window + 1)
    locked = power = ""
    tripped_at = None
    exited_at = None
    stopping = False
    gb_cg_reported = drm_reported = False
    tick = 0

    while True:
        started = time.time()
        tick += 1
        if gb_cg is None or not (gb_cg / "cgroup.procs").exists():
            cg = cgroup_of(child.pid)
            gb_cg = cg if cg is not None and cg.name.endswith(".scope") and unit in cg.name else None
            if gb_cg is not None and not gb_cg_reported:
                gb_cg_reported = True
                has_stat = (gb_cg / "memory.stat").exists()
                events.add("INFO", f"Grok Bot cgroup {gb_cg} memory.stat={'yes' if has_stat else 'missing'}")
            elif gb_cg is None and tick == 5 and not gb_cg_reported and child.poll() is None:
                gb_cg_reported = True
                events.add("INFO", f"Grok Bot cgroup not found; launcher pid {child.pid} is in {cg}")
        if gs_pid is None or not Path(f"/proc/{gs_pid}").exists():
            gs_pid = find_gnome_shell()
            if gs_pid:
                events.add("INFO", f"GNOME Shell pid {gs_pid} cgroup {cgroup_of(gs_pid)}")
        if tick % 2 == 1:
            locked, power = screen_state()

        mem = meminfo()
        gs_cg_shmem, gs_cg_current = cgroup_memory(cgroup_of(gs_pid)) if gs_pid else (None, None)
        gb_cg_shmem, gb_cg_current = cgroup_memory(gb_cg)
        gb_pids = grok_bot_pids(gb_cg)
        counts = tap.take()
        history.append((clock(), counts))
        window.update(counts)
        writer.writerow([
            clock(), mb(mem.get("MemAvailable")), mb(mem.get("AnonPages")),
            mb(mem.get("Shmem")), mb(mem.get("Unevictable")),
            gs_pid or "", mb(gs_cg_shmem), mb(gs_cg_current),
            mb(status_field(gs_pid, "VmSize")) if gs_pid else "",
            mb(status_field(gs_pid, "VmRSS")) if gs_pid else "",
            mb(gb_cg_shmem), mb(gb_cg_current), mb(sum(rss(p) for p in gb_pids)), len(gb_pids),
            locked, power, sum(counts.values()),
            counts.get("> wl_surface.commit", 0), counts.get("> wl_surface.attach", 0),
        ])
        if tick % 2 == 0:
            for key, n in window.most_common():
                wayland.write(f"{clock()}\t{key}\t{n}\n")
            window.clear()
        if tick % 5 == 0 and gs_pid:
            usage = drm_usage(gs_pid)
            for key, value in sorted(usage.items()):
                drm.write(f"{clock()}\t{key}\t{round(value / MB)}\n")
            if not usage and not drm_reported:
                drm_reported = True
                try:
                    os.listdir(f"/proc/{gs_pid}/fdinfo")
                    why = "readable, but no drm memory keys"
                except OSError as error:
                    why = f"unreadable ({error.strerror})"
                events.add("INFO", f"GNOME Shell fdinfo {why}; no GPU memory figures")

        watched = gs_cg_shmem if gs_cg_shmem is not None else mem.get("Shmem", 0)
        rises.append(watched)
        if tripped_at is None and exited_at is None and gb_pids:
            reason = None
            rise = watched - min(rises)
            if rise > args.trip_rise * MB:
                reason = (f"{'GNOME Shell' if gs_cg_shmem is not None else 'system'} shared memory "
                          f"rose {round(rise / MB)} MB in {len(rises) - 1} s")
            elif mem.get("MemAvailable", 1 << 62) < args.trip_avail * MB:
                reason = f"MemAvailable down to {round(mem['MemAvailable'] / MB)} MB"
            if reason:
                tripped_at = time.time()
                events.add("TRIP", f"{reason}; locked={locked or '?'} power_save={power or '?'}")
                try:
                    write_trip_snapshot(out, gs_pid, gb_pids, tap, history)
                    events.add("SNAPSHOT", str(out / "trip"))
                except Exception as error:  # the kill below matters more than the snapshot
                    events.add("SNAPSHOT", f"failed: {error!r}")
                if not args.no_kill:
                    stop_grok_bot(lambda: grok_bot_pids(gb_cg), events)

        if exited_at is None and child.poll() is not None and not gb_pids:
            exited_at = time.time()
            events.add("EXIT", f"Grok Bot exited with code {child.returncode}")
        if interrupted and not stopping:
            stopping = True
            if exited_at is None:
                events.add("STOP", f"harness got {interrupted[0]}; stopping Grok Bot")
                stop_grok_bot(lambda: grok_bot_pids(gb_cg), events)
        if exited_at is not None:
            settle = args.after_trip if tripped_at is not None and not interrupted else 5
            if time.time() - exited_at >= settle or len(interrupted) >= 2:
                break

        time.sleep(max(0.0, 1.0 - (time.time() - started)))

    stop.set()
    if journal is not None:
        journal.terminate()
    events.add("END", "")
    report = build_report(out)
    print(report)
    print(f"[harness] Report: {out / 'report.md'}")
    return 0


def read_tsv(path):
    try:
        with open(path, newline="") as handle:
            return list(csv.DictReader(handle, delimiter="\t"))
    except OSError:
        return []


def num(row, key):
    try:
        return float(row.get(key) or "")
    except ValueError:
        return None


def to_seconds(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def build_report(out):
    out = Path(out)
    rows = read_tsv(out / "metrics.tsv")
    events = read(out / "events.log").splitlines()
    lines = [f"# Grok Bot harness report: {out.name}", ""]

    lines.append("## Run")
    for event in events:
        if event.split(" ", 2)[1:2] and event.split(" ", 2)[1] in ("START", "INFO", "TRIP", "SNAPSHOT", "KILL", "EXIT", "STOP", "END"):
            lines.append(f"- `{event}`")
    if not any(" END" in e for e in events):
        lines.append("- run did not finish cleanly (session crash or harness killed)")
    lines.append("")

    if not rows:
        lines.append("No metrics were recorded.")
        text = "\n".join(lines) + "\n"
        (out / "report.md").write_text(text)
        return text

    key = "gs_cg_shmem_mb" if any(r.get("gs_cg_shmem_mb") for r in rows) else "shmem_mb"
    label = "GNOME Shell shmem" if key == "gs_cg_shmem_mb" else "system shmem"
    first = rows[: min(30, len(rows))]
    def median(field):
        values = [v for v in (num(r, field) for r in first) if v is not None]
        return round(statistics.median(values)) if values else None
    def peak(field, lowest=False):
        values = [(num(r, field), r["time"]) for r in rows if num(r, field) is not None]
        if not values:
            return None, ""
        return (min if lowest else max)(values)
    lines.append("## Memory (MB)")
    lines.append(f"Samples: {len(rows)} ({rows[0]['time']} to {rows[-1]['time']})")
    lines.append("")
    lines.append("| | first 30 s (median) | worst | at |")
    lines.append("|---|---|---|---|")
    for field, name, lowest in (
        ("mem_avail_mb", "MemAvailable", True), ("shmem_mb", "system Shmem", False),
        ("unevictable_mb", "Unevictable", False), ("gs_cg_shmem_mb", "GNOME Shell shmem", False),
        ("gs_vmsize_mb", "GNOME Shell VmSize", False), ("gb_rss_mb", "Grok Bot RSS (sum)", False),
        ("gb_cg_shmem_mb", "Grok Bot shmem", False),
    ):
        value, when = peak(field, lowest)
        if value is not None:
            lines.append(f"| {name} | {median(field)} | {round(value)} | {when} |")
    lines.append("")

    # Bursts: the watched value rising by more than 1 GB within 10 samples.
    bursts, start_index = [], None
    for i, row in enumerate(rows):
        now_v = num(row, key)
        past = [num(r, key) for r in rows[max(0, i - 10): i + 1]]
        past = [v for v in past if v is not None]
        rising = now_v is not None and past and now_v - min(past) > 1024
        if rising and start_index is None:
            # The burst starts at the low point the rise is measured from.
            first = max(0, i - 10)
            window_values = [(num(r, key), j) for j, r in enumerate(rows[first: i + 1], first)
                             if num(r, key) is not None]
            start_index = max(j for v, j in window_values if v == min(window_values)[0])
        elif not rising and start_index is not None:
            bursts.append((start_index, i - 1))  # row i is no longer rising
            start_index = None
    if start_index is not None:
        bursts.append((start_index, len(rows) - 1))
    lines.append(f"## Bursts ({label} +1 GB within 10 s)")
    if not bursts:
        lines.append("None.")
    else:
        lines.append("| start | end | from MB | to MB | 1 min later MB | locked | power_save | commits/s in burst | commits/s before |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for a, b in bursts[:15]:
            seg, before = rows[a: b + 1], rows[max(0, a - 30): a]
            def avg(part, field):
                values = [v for v in (num(r, field) for r in part) if v is not None]
                return f"{statistics.mean(values):.1f}" if values else "-"
            values = [num(r, key) for r in seg if num(r, key) is not None]
            end_s = to_seconds(rows[b]["time"])
            later = [num(r, key) for r in rows[b:]
                     if to_seconds(r["time"]) - end_s >= 60 and num(r, key) is not None]
            start_v = num(rows[a], key)
            lines.append(
                f"| {rows[a]['time']} | {rows[b]['time']} | "
                f"{round(start_v if start_v is not None else min(values))} | {round(max(values))} | "
                f"{round(later[0]) if later else '-'} | "
                f"{rows[a].get('locked') or '?'} | {rows[a].get('power_save') or '?'} | "
                f"{avg(seg, 'wl_commits')} | {avg(before, 'wl_commits')} |")
    lines.append("")

    kill = next((e for e in events if " KILL " in e), None)
    if kill:
        kill_s = to_seconds(kill.split()[0])
        at_kill = [num(r, key) for r in rows if to_seconds(r["time"]) <= kill_s and num(r, key) is not None]
        after = [num(r, key) for r in rows if 0 < to_seconds(r["time"]) - kill_s <= 120 and num(r, key) is not None]
        lines.append("## After stopping Grok Bot")
        if at_kill and after:
            lines.append(f"{label}: {round(at_kill[-1])} MB at the kill, lowest {round(min(after))} MB "
                         f"within 2 minutes, last {round(after[-1])} MB.")
        else:
            lines.append("Not enough samples after the kill.")
        lines.append("")

    locked_rows = [r for r in rows if r.get("locked") == "1"]
    lines.append("## Screen")
    lines.append(f"Locked in {len(locked_rows)} of {len(rows)} samples; displays off "
                 f"(power_save 3) in {sum(1 for r in rows if r.get('power_save') == '3')}.")
    lines.append("")

    wl = read_tsv(out / "wayland.tsv")
    if wl:
        seconds = max(len(rows), 1)
        overall = collections.Counter()
        for row in wl:
            overall[row["message"]] += int(row["count"])
        lines.append("## Wayland messages (per second, whole run; > sent, < received)")
        lines.extend(f"- {n / seconds:.1f}  `{k}`" for k, n in overall.most_common(12))
        focus = bursts[0][0] if bursts else None
        if focus is not None:
            focus_s = to_seconds(rows[focus]["time"])
            near = collections.Counter()
            for row in wl:
                if -30 <= to_seconds(row["time"]) - focus_s <= 10:
                    near[row["message"]] += int(row["count"])
            lines.append("")
            lines.append(f"Around the first burst ({rows[focus]['time']}, 30 s before to 10 s after):")
            lines.extend(f"- {n / 40:.1f}  `{k}`" for k, n in near.most_common(12))
        lines.append("")

    drm_rows = read_tsv(out / "drm.tsv")
    if drm_rows:
        last = {}
        for row in drm_rows:
            last[row["key"]] = (row["time"], row["mb"])
        lines.append("## GNOME Shell GPU memory (drm fdinfo, last sample)")
        lines.extend(f"- {k}: {mbv} MB ({t})" for k, (t, mbv) in sorted(last.items()))
        lines.append("")

    journal = read(out / "journal.log").splitlines()
    notable = [l for l in journal if re.search(
        r"oom|out of memory|killed process|terminated abnormally|SEGV|segfault|dumped|BO creation|VM_BIND|flip_done|commit wait",
        l, re.IGNORECASE)]
    lines.append("## Notable journal lines")
    lines.extend(f"    {l[:220]}" for l in notable[-25:]) if notable else lines.append("None.")
    lines.append("")
    lines.append("Files: metrics.tsv, wayland.tsv, drm.tsv, journal.log, events.log, trip/, grok-bot.log")

    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text)
    return text


def main():
    parser = argparse.ArgumentParser(
        description="Run Grok Bot under a memory watchdog and record metrics.",
        epilog="Arguments after -- go to Grok Bot, e.g. -- --ozone-platform=x11",
    )
    parser.add_argument("--grok-bot", default="grok-bot", help="Grok Bot launcher to run")
    parser.add_argument("--out", help="run directory (default ~/grok-bot-harness/<date-time>)")
    parser.add_argument("--trip-rise", type=int, default=2048,
                        help="trip when GNOME Shell shmem rises this many MB ... (default 2048)")
    parser.add_argument("--trip-window", type=int, default=10,
                        help="... within this many seconds (default 10)")
    parser.add_argument("--trip-avail", type=int, default=2048,
                        help="also trip when MemAvailable drops below this many MB (default 2048)")
    parser.add_argument("--after-trip", type=int, default=90,
                        help="seconds to keep measuring after stopping Grok Bot (default 90)")
    parser.add_argument("--no-kill", action="store_true",
                        help="record the trip but leave Grok Bot running")
    parser.add_argument("--no-wayland-debug", action="store_true",
                        help="do not trace Wayland messages")
    parser.add_argument("--report", metavar="DIR", help="only rebuild report.md for a past run")
    parser.add_argument("grok_args", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.report:
        print(build_report(Path(args.report).expanduser()))
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
