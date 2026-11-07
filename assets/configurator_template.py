#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
configurator_template.py — starting point for "one-click install" tooling of a C
library into an embedded project.

Copy this file next to the library sources, fill in config.json, and edit the
blocks marked `TODO(project-specific)`. The generic parts below are the ones you
keep: backup, idempotent anchored insertion, .uvprojx editing, dry-run, GUI.

    python configurator_template.py                 # GUI
    python configurator_template.py --project PATH  # CLI
    python configurator_template.py --project PATH --dry-run

config.json schema
------------------
{
  "name": "mylib",
  "marker": "MYLIB_AUTOCONFIG",          // idempotency tag, must be unique
  "backup_dir": "MYLIB_backup",           // under <project>/MDK-ARM/
  "files": [                              // library sources -> project paths
    {"src": "mylib.c", "dst": "bsp/mylib/mylib.c"},
    {"src": "mylib.h", "dst": "bsp/mylib/mylib.h"}
  ],
  "inserts": [                            // anchored, idempotent text edits
    {
      "file": "Core/Src/freertos.c",
      "anchor": "/* USER CODE BEGIN RTOS_THREADS */",
      "snippet": "  osThreadNew(APP_MyLib_Task, NULL, &mylib_attributes);"
    }
  ],
  "uvprojx": {                            // Keil project registration
    "group": "bsp_upper",
    "files": ["..\\\\bsp\\\\mylib\\\\mylib.c"],
    "include": "../bsp/mylib"
  }
}

Rules that make it safe (see references/patch-playbook.md):
  * never edit a file without copying it to <backup_dir>/<timestamp>/ first
  * every insertion carries the marker so a second run is a no-op
  * anchors are CubeMX USER CODE markers, so regeneration keeps the edits
  * on any write failure the original file is restored from the backup
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent
CONFIG_FILE = LIB_DIR / "config.json"
VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# text helpers — keep line endings and encoding exactly as found
# ---------------------------------------------------------------------------
def read_text(path: Path) -> str:
    # utf-8-sig drops a BOM if present; editors/PowerShell love adding one and an
    # XML parser rejects "﻿<?xml ..." with a confusing "invalid token" error.
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def nl_of(text: str) -> str:
    """Dominant line ending (majority vote; files edited by two OSes have both)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def load_config() -> dict:
    if not CONFIG_FILE.is_file():
        raise SystemExit(f"missing {CONFIG_FILE.name} next to this script")
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------
@dataclass
class Report:
    changes: list = field(default_factory=list)   # (action, path, backup)
    warnings: list = field(default_factory=list)
    backup_root: Path | None = None


class Installer:
    def __init__(self, project: Path, cfg: dict, log=print, dry_run: bool = False):
        self.cfg = cfg
        self.log = log
        self.dry_run = dry_run
        self.report = Report()
        self.marker = cfg["marker"]
        self.mdkarm: Path | None = None
        self.root: Path | None = None
        self.project_arg = Path(project)
        self.uvprojx: Path | None = None

    # ---- discovery -------------------------------------------------------
    def resolve(self) -> None:
        p = self.project_arg.resolve()
        if not p.exists():
            raise FileNotFoundError(f"no such path: {p}")

        candidates = sorted(p.glob("*.uvprojx"))
        if candidates:
            self.mdkarm, self.uvprojx = p, candidates[0]
            self.root = p.parent
        else:
            mdk = p / "MDK-ARM"
            if mdk.is_dir() and list(mdk.glob("*.uvprojx")):
                self.mdkarm, self.uvprojx = mdk, sorted(mdk.glob("*.uvprojx"))[0]
                self.root = p
            else:
                self.root = p                      # CMake / Make project
        if not (self.root / "Core").is_dir() and not (self.root / "src").is_dir():
            self.report.warnings.append(
                "neither Core/ nor src/ found — check that this is the project root")
        self.log(f"project root : {self.root}")
        if self.uvprojx:
            self.log(f"keil project : {self.uvprojx.name}")

    # ---- backup ----------------------------------------------------------
    def backup(self, path: Path) -> Path:
        if self.report.backup_root is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = (self.mdkarm or self.root) / self.cfg.get("backup_dir", "MYLIB_backup")
            self.report.backup_root = base / ts
        rel = path.resolve().relative_to(self.root.resolve())
        dst = self.report.backup_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        return dst

    # ---- 1) copy library files ------------------------------------------
    def copy_files(self) -> None:
        for entry in self.cfg.get("files", []):
            src = LIB_DIR / entry["src"]
            if not src.is_file():
                raise FileNotFoundError(f"library file missing: {src.name}")
            dst = self.root / entry["dst"]
            text = read_text(src)
            if dst.is_file() and read_text(dst) == text:
                self.log(f"[same] {entry['dst']}")
                self.report.changes.append(("unchanged", entry["dst"], None))
                continue
            if self.dry_run:
                self.log(f"[dry ] write {entry['dst']}")
                self.report.changes.append(("would-write", entry["dst"], None))
                continue
            bak = self.backup(dst) if dst.is_file() else None
            write_text(dst, text)
            self.log(f"[write] {entry['dst']}" + ("  (backup)" if bak else ""))
            self.report.changes.append(("written", entry["dst"], bak))

    # ---- 2) anchored insertions -----------------------------------------
    def insert_after_line(self, text: str, anchor: str, snippet: str) -> str | None:
        """Put `snippet` on its own line(s) right after the anchor line.

        The line end is located with "\\n", never with "\\r\\n": files saved on
        Windows are CRLF, files from CubeMX on Linux are LF, and a file that was
        edited by two tools can contain *both*. Searching for "\\r\\n" then
        silently appends at EOF (the first CRLF may be far below the anchor).
        """
        idx = text.find(anchor)
        if idx < 0:
            return None
        line_end = text.find("\n", idx)
        if line_end < 0:
            return None
        nl = nl_of(text)
        pos = line_end + 1
        return text[:pos] + snippet + nl + text[pos:]

    def apply_inserts(self) -> None:
        for entry in self.cfg.get("inserts", []):
            path = self.root / entry["file"]
            if not path.is_file():
                self.report.warnings.append(f"skip insert: {entry['file']} not found")
                self.log(f"[skip] {entry['file']} (not found)")
                continue
            text = read_text(path)
            if self.marker in text:
                self.log(f"[same] {entry['file']} (already patched)")
                self.report.changes.append(("unchanged", entry["file"], None))
                continue
            snippet = f"/* {self.marker} */\n{entry['snippet']}"
            new_text = self.insert_after_line(text, entry["anchor"], snippet)
            if new_text is None:
                self.report.warnings.append(
                    f"anchor not found in {entry['file']}: {entry['anchor']}")
                self.log(f"[skip] {entry['file']} (anchor missing)")
                continue
            if self.dry_run:
                self.log(f"[dry ] patch {entry['file']}")
                self.report.changes.append(("would-patch", entry["file"], None))
                continue
            bak = self.backup(path)
            write_text(path, new_text)
            self.log(f"[patch] {entry['file']}" + ("  (backup)" if bak else ""))
            self.report.changes.append(("patched", entry["file"], bak))

    # ---- 3) Keil project registration ------------------------------------
    def add_uvprojx_file(self, text: str, name: str, fpath: str) -> tuple[str, bool]:
        """Insert a <File> entry into the configured group, at line start."""
        nl = nl_of(text)
        entry = (f"            <File>{nl}"
                 f"              <FileName>{name}</FileName>{nl}"
                 f"              <FileType>1</FileType>{nl}"
                 f"              <FilePath>{fpath}</FilePath>{nl}"
                 f"            </File>{nl}")
        group = self.cfg["uvprojx"]["group"]
        starts = [m.start() for m in
                  re.finditer(rf"<Group>\s*<GroupName>{re.escape(group)}</GroupName>", text)]
        if not starts:
            block = (f"        <Group>{nl}"
                     f"          <GroupName>{group}</GroupName>{nl}"
                     f"          <Files>{nl}{entry}          </Files>{nl}"
                     f"        </Group>{nl}")
            idx = text.find("</Groups>")
            return (text[:idx] + block + text[idx:], True) if idx >= 0 else (text, False)

        changed = False
        for g_start in reversed(starts):
            g_end = text.find("</Group>", g_start)
            if g_end < 0 or f"<FileName>{name}</FileName>" in text[g_start:g_end]:
                continue
            files_close = text.find("</Files>", g_start, g_end)
            if files_close < 0:
                continue
            # Line start must be found with "\n": a "\r\n" search silently
            # returns -1 on an LF file and the entry lands at offset 0.
            line_start = text.rfind("\n", 0, files_close) + 1
            text = text[:line_start] + entry + text[line_start:]
            changed = True
        return text, changed

    def add_include(self, text: str, entry: str) -> tuple[str, bool]:
        changed = False

        def repl(m: re.Match) -> str:
            nonlocal changed
            value = m.group(2).strip()
            if not value or "../Core/Inc" not in value:      # only the main target
                return m.group(0)
            parts = [x.strip() for x in value.split(";") if x.strip()]
            if entry in parts:
                return m.group(0)
            changed = True
            return f"{m.group(1)}{value};{entry}{m.group(3)}"

        return re.sub(r"(<IncludePath>)([^<]*)(</IncludePath>)", repl, text), changed

    def patch_uvprojx(self) -> None:
        if not self.uvprojx:
            return
        text = read_text(self.uvprojx)
        changed = False
        added = []
        for fpath in self.cfg.get("uvprojx", {}).get("files", []):
            name = Path(fpath.replace("\\", "/")).name
            text, ok = self.add_uvprojx_file(text, name, fpath)
            changed |= ok
            if ok:
                added.append(name)
        inc = self.cfg.get("uvprojx", {}).get("include")
        if inc:
            text, ok = self.add_include(text, inc)
            changed |= ok
            if ok:
                added.append(f"IncludePath += {inc}")
        if not changed:
            self.log("[same] uvprojx already registered")
            return
        ET.fromstring(text.encode("utf-8"))                 # refuse to write broken XML
        if self.dry_run:
            self.log(f"[dry ] uvprojx: {', '.join(added)}")
            self.report.changes.append(("would-patch", self.uvprojx.name, None))
            return
        bak = self.backup(self.uvprojx)
        write_text(self.uvprojx, text)
        self.log(f"[patch] {self.uvprojx.name}: {', '.join(added)}")
        self.report.changes.append(("patched", self.uvprojx.name, bak))

    # ---- run -------------------------------------------------------------
    def run(self) -> Report:
        self.resolve()
        self.copy_files()
        self.apply_inserts()
        self.patch_uvprojx()
        # TODO(project-specific): peripheral bring-up (UART/DMA/IRQ/NVIC) and
        # thread creation belong here, as generated port files plus anchored
        # insertions — see references/patch-playbook.md §3 and §5.
        if not self.dry_run and self.report.backup_root and self.report.backup_root.is_dir():
            lines = [f"{self.cfg.get('name', 'lib')} install report",
                     f"time  : {datetime.now():%Y-%m-%d %H:%M:%S}",
                     f"root  : {self.root}",
                     f"dry   : {self.dry_run}",
                     ""]
            lines += [f"[{a}] {p}" + (f"  backup={b}" if b else "")
                      for a, p, b in self.report.changes]
            lines += ["", "warnings:"] + [f"  * {w}" for w in self.report.warnings]
            (self.report.backup_root / "install_report.txt").write_text(
                "\n".join(lines), encoding="utf-8")
        return self.report


# ---------------------------------------------------------------------------
# GUI (tkinter, stdlib) — same installer, one button
# ---------------------------------------------------------------------------
def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    cfg = load_config()
    name = cfg.get("name", "library")

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"{name} installer  v{VERSION}")
            self.geometry("820x600")
            self.path = tk.StringVar()
            self.dry = tk.BooleanVar(value=False)
            ttk.Label(self, text=f"1. project directory (MDK-ARM or project root)").pack(
                anchor="w", padx=10, pady=(10, 0))
            row = ttk.Frame(self)
            row.pack(fill="x", padx=10, pady=4)
            ttk.Entry(row, textvariable=self.path).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Browse", command=self.browse, width=9).pack(side="left", padx=6)
            ttk.Checkbutton(self, text="dry run (check only, write nothing)",
                            variable=self.dry).pack(anchor="w", padx=10)
            btns = ttk.Frame(self)
            btns.pack(fill="x", padx=10, pady=6)
            self.go = ttk.Button(btns, text="Install", command=self.install)
            self.go.pack(side="left")
            ttk.Button(btns, text="Quit", command=self.destroy).pack(side="right")
            self.log_box = scrolledtext.ScrolledText(self, font=("Consolas", 9))
            self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            self.log_box.configure(state="disabled")

        def browse(self):
            p = filedialog.askdirectory()
            if p:
                self.path.set(p)

        def log(self, msg):
            self.log_box.configure(state="normal")
            self.log_box.insert("end", str(msg) + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
            self.update_idletasks()

        def install(self):
            if not self.path.get().strip():
                messagebox.showwarning("?", "choose the project directory first")
                return
            self.go.configure(state="disabled")
            try:
                inst = Installer(Path(self.path.get().strip()), cfg,
                                 log=self.log, dry_run=self.dry.get())
                rep = inst.run()
                changed = [c for c in rep.changes if c[0] in ("written", "patched")]
                self.log("-" * 50)
                self.log(f"done: {len(changed)} change(s)"
                         + (f", backup at {rep.backup_root}" if rep.backup_root else ""))
                messagebox.showinfo("done", f"{len(changed)} change(s)")
            except Exception as exc:                       # noqa: BLE001
                self.log(f"[error] {exc}")
                self.log(traceback.format_exc())
                messagebox.showerror("failed", str(exc))
            finally:
                self.go.configure(state="normal")

    App().mainloop()
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Install a C library into an embedded project.")
    ap.add_argument("--project", help="project root or MDK-ARM directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--version", action="version", version=f"configurator_template {VERSION}")
    args = ap.parse_args()
    if not args.project:
        return run_gui()
    cfg = load_config()
    inst = Installer(Path(args.project), cfg, dry_run=args.dry_run)
    try:
        rep = inst.run()
    except Exception as exc:                               # noqa: BLE001
        print(f"[error] {exc}")
        return 1
    for w in rep.warnings:
        print(f"[warn] {w}")
    print("done." + (" (dry run)" if args.dry_run else ""))
    return 0


# TODO(project-specific): the pieces this template deliberately does NOT do —
#   1. generate the port layer (.c/.h) for the library's peripherals
#   2. detect "project already has this integration" and refuse to double-wire
#   3. patch existing HAL callbacks (one forwarding line each)
# See references/patch-playbook.md for the exact patterns used to build these.

if __name__ == "__main__":
    sys.exit(main())
