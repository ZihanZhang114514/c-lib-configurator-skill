#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_project.py — inspect an embedded C project before wiring a library into it.

Read-only. Answers the questions you must answer before touching any file:

  * which build system(s) drive the project (.uvprojx / CMake / Makefile)
  * where the sources and include paths are
  * which UART / DMA / IRQ resources are already taken (so the new driver gets a
    free DMA stream and does not duplicate an interrupt handler)
  * which HAL callbacks already exist (so the driver extends them instead of
    defining a second copy)
  * whether the project is CubeMX-generated (USER CODE anchors) and which
    top-level directories are vendor code (skip them)

Usage:
    python scan_project.py <project-or-MDK-dir> [--json] [--grep REGEX] [--quiet]

Examples:
    python scan_project.py D:\\27RC\\dm_demo
    python scan_project.py D:\\27RC\\dm_demo\\MDK-ARM --grep "Unitree_GO_|APP_Unitree"
    python scan_project.py D:\\proj --json > scan.json

Exit codes: 0 ok, 2 nothing that looks like a C project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Directories that are vendor code or build output: scanned only for the
# "already used" resource tables when explicitly needed, never listed as
# candidate source dirs.
VENDOR_DIRS = {"drivers", "middlewares", "cmsis", "hal_driver", "third_party",
               "thirdparty", "vendor", "libs"}
SKIP_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules", "build",
             "out", "debug", "release", "obj", "bin", "rte", ".vs", ".vscode"}
SOURCE_SUFFIXES = (".c", ".h")
BUILD_FILES = ("*.uvprojx", "CMakeLists.txt", "Makefile", "makefile", "*.mk")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def strip_comments(text: str) -> str:
    """Comments removed; strings kept (identifiers inside them are rare and harmless)."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def walk(root: Path, skip_vendor: bool) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        low = str(rel).lower()
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS
                       and not d.startswith(".git")
                       and not (skip_vendor and rel.parts and
                                rel.parts[0].lower() in VENDOR_DIRS)]
        if low == ".":
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(SOURCE_SUFFIXES):
                out.append(Path(dirpath) / name)
    return out


# ---------------------------------------------------------------------------
# build system
# ---------------------------------------------------------------------------
def parse_uvprojx(path: Path) -> dict:
    """Best-effort read of a Keil MDK v5 project (XML)."""
    info = {
        "path": str(path),
        "targets": [],
        "device": "",
        "include_paths": [],
        "groups": {},
        "file_count": 0,
    }
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return info
    root = tree.getroot()
    for target in root.iter("Target"):
        name = (target.findtext("TargetName") or "").strip()
        if name:
            info["targets"].append(name)
        dev = target.findtext("./TargetOption/TargetCommonOption/Device")
        if dev and not info["device"]:
            info["device"] = dev.strip()
    for inc in root.iter("IncludePath"):
        text = (inc.text or "").strip()
        if not text:
            continue
        for part in text.split(";"):
            part = part.strip()
            if part and part not in info["include_paths"]:
                info["include_paths"].append(part)
    for group in root.iter("Group"):
        gname = (group.findtext("GroupName") or "").strip()
        if not gname:
            continue
        files = []
        for f in group.iter("File"):
            fname = (f.findtext("FileName") or "").strip()
            if fname:
                files.append(fname)
        info["groups"][gname] = files
        info["file_count"] += len(files)
    return info


def parse_cmake(text: str) -> dict:
    inc = []
    for m in re.finditer(r"include_directories\s*\(([^)]*)\)", text):
        inc += m.group(1).split()
    for m in re.finditer(r"target_include_directories\s*\(\s*\w+[^)]*?PRIVATE([^)]*)\)", text, re.S):
        inc += m.group(1).split()
    sources = []
    for m in re.finditer(r"add_executable\s*\(([^)]*)\)", text, re.S):
        sources += [s for s in m.group(1).split() if s.endswith(".c")]
    for m in re.finditer(r"add_library\s*\(([^)]*)\)", text, re.S):
        sources += [s for s in m.group(1).split() if s.endswith(".c")]
    return {"include_paths": sorted(set(inc)), "sources": sorted(set(sources))}


def parse_makefile(text: str) -> dict:
    inc = sorted(set(re.findall(r"-I\s*([^\s\\]+)", text)))
    sources = sorted(set(re.findall(r"([\w./-]+\.c)\b", text)))
    return {"include_paths": inc, "sources": sources}


def find_build(root: Path) -> dict:
    found = {"uvprojx": [], "cmake": [], "make": []}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) > 3:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith(".git")]
        for name in filenames:
            p = Path(dirpath) / name
            low = name.lower()
            if low.endswith(".uvprojx"):
                found["uvprojx"].append(p)
            elif name == "CMakeLists.txt":
                found["cmake"].append(p)
            elif low in ("makefile", "gnumakefile") or low.endswith(".mk"):
                found["make"].append(p)
    return found


# ---------------------------------------------------------------------------
# resource detection
# ---------------------------------------------------------------------------
def detect_uart(texts: dict) -> list:
    """Every USART/UART instance the project already uses."""
    blob = strip_comments("\n".join(texts.values()))
    handles = sorted(set(re.findall(r"UART_HandleTypeDef\s+(huart\w+)", blob)))
    inits = sorted(set(re.findall(r"\bMX_(UART\d|USART\d+)_UART_Init\s*\(", blob)))
    used = sorted(set(re.findall(r"\b(UART\d|USART\d+|LPUART\d)\b", blob)))
    return [{"handles": handles,
             "init_functions": [f"MX_{n}_UART_Init" for n in inits],
             "instances": used}]


def detect_dma(texts: dict) -> dict:
    blob = strip_comments("\n".join(texts.values()))
    streams = sorted(set(re.findall(r"\b(DMA[12]_Stream\d+)\b", blob)))
    requests = sorted(set(re.findall(r"\b(DMA_REQUEST_[A-Z0-9_]+)\b", blob)))
    return {"streams_used": streams, "requests_used": requests}


def free_streams(used: list, controllers=("DMA1", "DMA2"), n_streams=8) -> list:
    used_set = set(used)
    return [f"{c}_Stream{i}" for c in controllers for i in range(n_streams)
            if f"{c}_Stream{i}" not in used_set]


def detect_irq_handlers(texts: dict) -> dict:
    out = {}
    for rel, txt in texts.items():
        code = strip_comments(txt)
        for m in re.finditer(r"\bvoid\s+(\w+_IRQHandler)\s*\(\s*void\s*\)\s*\{", code):
            out[m.group(1)] = rel
    return out


CALLBACK_NAMES = (
    "HAL_UARTEx_RxEventCallback", "HAL_UART_ErrorCallback",
    "HAL_UART_RxCpltCallback", "HAL_UART_TxCpltCallback",
    "HAL_UART_AbortReceiveCpltCallback",
    "HAL_GPIO_EXTI_Callback", "HAL_TIM_PeriodElapsedCallback",
    "HAL_CAN_RxFifo0MsgPendingCallback", "HAL_FDCAN_RxFifo0Callback",
    "HAL_SPI_RxCpltCallback", "HAL_I2C_MasterTxCpltCallback",
    "HAL_DMA_IRQHandler",
)


def detect_callbacks(texts: dict) -> dict:
    out = {}
    for rel, txt in texts.items():
        code = strip_comments(txt)
        for name in CALLBACK_NAMES:
            if re.search(rf"\bvoid\s+{name}\s*\(", code):
                out.setdefault(name, []).append(rel)
    return out


def detect_cubemx(root: Path, texts: dict) -> dict:
    ioc = [str(p.relative_to(root)) for p in root.glob("*.ioc")]
    anchors = {}
    for rel, txt in texts.items():
        hits = re.findall(r"/\* USER CODE BEGIN (\w+) \*/", txt)
        if hits:
            anchors.setdefault(rel, sorted(set(hits)))
    return {
        "ioc": ioc,
        "generated_files_with_user_code": len(anchors),
        "anchors": anchors,
    }


def detect_rtos(texts: dict) -> dict:
    blob = strip_comments("\n".join(texts.values()))
    return {
        "cmsis_os": "cmsis_os.h" in blob or "cmsis_os2.h" in blob,
        "osThreadNew": len(re.findall(r"\bosThreadNew\s*\(", blob)),
        "freertos_c": any(rel.lower().endswith("freertos.c") for rel in texts),
        "task_functions": sorted(set(re.findall(r"\bvoid\s+(\w+)\s*\(\s*void\s*\*\s*\w+\s*\)", blob))),
    }


def source_dirs(root: Path, files: list) -> list:
    counts = {}
    for p in files:
        rel = p.relative_to(root)
        key = str(rel.parent) if len(rel.parts) > 1 else "."
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# ---------------------------------------------------------------------------
# main report
# ---------------------------------------------------------------------------
def scan(path: Path, grep: str | None, quiet: bool) -> dict:
    path = path.resolve()
    build = find_build(path)

    # Find the project root: the MDK-ARM dir's parent when only .uvprojx matched.
    root = path
    if build["uvprojx"] and all(p.parent.name.upper() == "MDK-ARM" for p in build["uvprojx"]):
        parents = {p.parent.parent for p in build["uvprojx"]}
        if len(parents) == 1:
            root = parents.pop()

    files = walk(root, skip_vendor=True)
    texts = {}
    for p in files:
        try:
            if p.stat().st_size <= 2_000_000:
                texts[str(p.relative_to(root))] = read_text(p)
        except OSError:
            continue

    uv = {}
    for p in build["uvprojx"][:1]:
        uv = parse_uvprojx(p)
    cmake = {}
    for p in build["cmake"][:3]:
        cmake.update(parse_cmake(read_text(p)))
    make = {}
    for p in build["make"][:3]:
        make.update(parse_makefile(read_text(p)))

    report = {
        "input": str(path),
        "project_root": str(root),
        "build": {
            "keil": [str(p.relative_to(root)) for p in build["uvprojx"]],
            "cmake": [str(p.relative_to(root)) for p in build["cmake"]],
            "make": [str(p.relative_to(root)) for p in build["make"]],
        },
        "keil_project": uv,
        "include_paths": sorted(set(
            uv.get("include_paths", []) + cmake.get("include_paths", []) + make.get("include_paths", []))),
        "source_dirs": source_dirs(root, files),
        "file_count": len(files),
        "uart": detect_uart(texts),
        "dma": detect_dma(texts),
        "irq_handlers": detect_irq_handlers(texts),
        "hal_callbacks": detect_callbacks(texts),
        "cubemx": detect_cubemx(root, texts),
        "rtos": detect_rtos(texts),
        "grep": {},
    }
    report["dma"]["streams_free"] = free_streams(report["dma"]["streams_used"])

    if grep:
        try:
            rx = re.compile(grep)
        except re.error as exc:
            report["grep"] = {"error": str(exc)}
        else:
            hits = {}
            for rel, txt in texts.items():
                n = len(rx.findall(strip_comments(txt)))
                if n:
                    hits[rel] = n
            report["grep"] = {"pattern": grep, "hits": hits}

    if not quiet:
        print_report(report)
    return report


def print_report(r: dict) -> None:
    p = print
    p(f"project root : {r['project_root']}")
    p(f"input        : {r['input']}")
    b = r["build"]
    systems = ", ".join(f"{k}:{len(v)}" for k, v in b.items() if v) or "none found"
    p(f"build system : {systems}")
    if r["keil_project"]:
        k = r["keil_project"]
        p(f"keil device  : {k.get('device') or '?'}   target(s): {', '.join(k.get('targets', [])) or '?'}")
        p(f"keil groups  : " + ", ".join(f"{g}({len(f)})" for g, f in k.get("groups", {}).items()))
    p(f"sources      : {r['file_count']} .c/.h files (vendor dirs skipped)")
    p("source dirs  : " + ", ".join(f"{d}({n})" for d, n in r["source_dirs"][:10]))
    p(f"includes     : {len(r['include_paths'])}")
    for inc in r["include_paths"][:12]:
        p(f"               {inc}")
    if len(r["include_paths"]) > 12:
        p(f"               ... (+{len(r['include_paths']) - 12})")

    u = r["uart"][0]
    p(f"uart handles : {', '.join(u['handles']) or 'none'}")
    p(f"uart inits   : {', '.join(u['init_functions']) or 'none'}")
    p(f"uart insts   : {', '.join(u['instances']) or 'none'}")

    d = r["dma"]
    p(f"dma streams  : used={', '.join(d['streams_used']) or 'none'}")
    p(f"dma requests : {', '.join(d['requests_used']) or 'none'}")
    p(f"dma free     : {', '.join(d['streams_free'][:8])}{' ...' if len(d['streams_free']) > 8 else ''}")

    irq = r["irq_handlers"]
    p(f"irq handlers : {len(irq)}  ({', '.join(sorted(irq)[:12])}{' ...' if len(irq) > 12 else ''})")

    cbs = r["hal_callbacks"]
    p(f"hal callbacks: {', '.join(sorted(cbs)) or 'none'}")

    c = r["cubemx"]
    p(f"cubemx       : ioc={c['ioc'] or 'none'}, files with USER CODE={c['generated_files_with_user_code']}")

    rt = r["rtos"]
    p(f"rtos         : cmsis_os={rt['cmsis_os']} freertos.c={rt['freertos_c']} osThreadNew x{rt['osThreadNew']}")

    g = r["grep"]
    if g:
        if "error" in g:
            p(f"grep         : invalid regex ({g['error']})")
        else:
            p(f"grep         : '{g['pattern']}' -> {len(g['hits'])} file(s)")
            for rel, n in sorted(g["hits"].items(), key=lambda kv: -kv[1])[:15]:
                p(f"               {rel}  x{n}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Scan an embedded C project before wiring a library into it.")
    ap.add_argument("path", help="project root, or the MDK-ARM directory containing the .uvprojx")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a human report")
    ap.add_argument("--grep", help="regex to look for in project sources (e.g. an existing integration)")
    ap.add_argument("--quiet", action="store_true", help="no human report (with --json)")
    args = ap.parse_args()

    target = Path(args.path)
    if not target.exists():
        print(f"error: no such path: {target}", file=sys.stderr)
        return 2
    report = scan(target, args.grep, quiet=args.quiet or args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["file_count"] == 0 and not report["build"]["keil"]:
        print("warning: nothing here looks like a C project (.c/.h + build file)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
