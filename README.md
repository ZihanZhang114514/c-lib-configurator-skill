# c-lib-configurator

**简体中文** | [English](#english)

> 一个把 C 库接进已有嵌入式工程（Keil MDK / CMake / Make）的技能，也用来写"输入路径就能装"的 Python 一键配置器。

---

## 中文

### 这是什么

一个给 AI 编码代理用的 **skill**：当你说"这个目录里是某个 C 库的文件，帮我配到工程里"，或者"给我写个自动配库器"，它会按一套验证过的流程做事：

1. **先读库**：头文件、依赖、需要的外设/DMA/中断/回调/线程；
2. **再读工程**（只读，不改）：构建系统、源目录、包含路径、哪些 UART/DMA/IRQ 回调已经被占用、是否 CubeMX 生成、是否已有同类接入代码；
3. **决定"复用还是生成"**：工程已有 `huart2` 就复用并校正参数，没有就生成移植层；中断函数、HAL 回调只在工程里没有时才生成；
4. **补丁四原则**：锚点插入、幂等标记、写前备份、写完校验；
5. **交付**：改好的工程（或配置器）+ 备份 + 报告，并明确写清"没编译、没上机"。

它来自一次真实任务：把宇树 GO-M8010-6 电机驱动接进 STM32H723 + Keil + FreeRTOS 工程，后来又把接收从阻塞改成 DMA + 空闲中断。所有踩过的坑都写在 `references/` 和 `examples/` 里。

### 适合 / 不适合

**适合**

- 手里有 `.c/.h`，要接进已有的 CubeMX/Keil 工程；
- 要给某个库写"一键安装/一键配置"工具（GUI 或 CLI）；
- 想知道"改哪些文件、改哪里、怎么保证不把工程改坏"。

**不适合**

- 从零新建工程（那是 CubeMX 的活）；
- 需要改 HAL 驱动本身、需要交叉编译验证（这里没有编译器，只做静态验证）；
- 需要商业库/闭源 SDK 的许可处理。

### 安装

```powershell
git clone https://github.com/ZihanZhang114514/c-lib-configurator-skill.git
cd c-lib-configurator-skill
.\scripts\install-skill.ps1 -Target dsh        # 装到 %USERPROFILE%\.dsh\skills\
.\scripts\install-skill.ps1 -Target all        # 同时装到 .claude\skills\
```

装完重启会话（或在 Web GUI 里开新会话），技能列表里出现 `c-lib-configurator` 即可。

手工安装也行：把整个目录复制到 `%USERPROFILE%\.dsh\skills\c-lib-configurator\`，`SKILL.md` 必须在根。

### 目录结构

```
c-lib-configurator/
├── SKILL.md                        # 技能本体：流程、决策矩阵、四原则、错误清单
├── README.md                       # 本文件（中英双语）
├── scripts/
│   ├── scan_project.py             # 只读扫描器：构建系统、占用资源、回调、锚点
│   └── install-skill.ps1           # 安装到 DSH / Claude Code
├── assets/
│   └── configurator_template.py    # 配置器骨架（GUI+CLI+备份+锚点插入+uvprojx）
├── references/
│   ├── patch-playbook.md           # 补丁细节：换行/BOM/锚点/uvprojx/回调/DMA
│   └── checklist.md                # 动手前、交付前逐条打勾
└── examples/
    └── stm32-keil-uart-dma.md      # 真实案例 + 6 个踩坑记录
```

### 快速开始

**第一步：扫工程（只读）**

```powershell
python scripts\scan_project.py "D:\proj"
python scripts\scan_project.py "D:\proj\MDK-ARM" --json
python scripts\scan_project.py "D:\proj" --grep "MyLib_"
```

输出会告诉你：构建系统、工程根目录、源目录、包含路径、已用/空闲的 DMA stream、已定义的中断函数、已定义的 HAL 回调、是否 CubeMX、是否 RTOS。

**第二步：直接配置 / 或写配置器**

- 直接配置：按 `SKILL.md` 的流程改 4~5 个文件（先备份）；
- 写配置器：复制 `assets/configurator_template.py`，填 `config.json`，只改 `TODO(project-specific)` 两处。

**第三步：验证（没有编译器也能做）**

沙箱副本跑一遍 → 结构检查（括号/注释）→ XML 校验 → HAL 符号核对 → 四种工程形态回归 → 重跑确认幂等。

### 它解决的具体问题

| 问题 | 这里给的答案 |
|---|---|
| 该改哪些文件？ | 驱动目录、已有的 HAL 回调文件、`freertos.c`、`.uvprojx`（+ 可选的移植层） |
| 怎么保证 CubeMX 重新生成不丢？ | 只插 `USER CODE` 区，其余放新文件 |
| 怎么保证重跑不出事？ | marker + 归一化比较 + 全量备份 |
| 工程里已经有一份接入代码怎么办？ | 扫描前缀，命中就只同步、不接线 |
| 中断函数/回调重复定义？ | 先查 `*_IRQHandler` 和 `HAL_*Callback`，有就不生成，改为插一行转发 |
| 没有交叉编译器怎么验？ | 沙箱 + 结构检查 + XML 解析 + HAL 符号 grep |

### 踩过的坑（都收进 `references/` 了）

1. 混合换行 + `find("\r\n")` → 补丁插到了文件末尾；
2. PowerShell 写的 UTF-8 BOM → `ET.fromstring` 报 `invalid token`；
3. 在 `</Files>` 前拼字符串 → 缩进错位、`</Files>` 掉到行首；
4. 两份接入代码同时存在 → `g_go_joint` 重复定义 + 两个任务抢总线；
5. 复用 `huart2` 却不校正波特率 → 能发不能收；
6. 示例任务默认就动电机 → 用户没预期（改成先零力矩 2 s）。

### 许可与归属

无许可证文件即保留所有权利；仓库内容为本项目的工作笔记，可自由参考思路。`Unitree_GO` 驱动本身另见 `Unitree_GO_dm_lib` 仓库。

---

## English

### What this is

A **skill** for AI coding agents. When you say *"this folder holds a C library — wire it into my project"* or *"write me a one-click installer"*, the agent follows a tested workflow:

1. **Read the library first** — headers, dependencies, the peripherals/DMA/IRQs/callbacks/threads it needs.
2. **Read the project (read-only)** — build system, source dirs, include paths, which UARTs/DMA streams/IRQ handlers/HAL callbacks are already taken, whether it is CubeMX-generated, whether some earlier integration already exists.
3. **Decide reuse-vs-generate** — reuse an existing `huart2` (and correct its parameters), otherwise generate a port layer; only define interrupt handlers and HAL callbacks when the project does not already have them.
4. **Four patching rules** — anchor-based insertion, idempotency marker, backup before write, validate after write.
5. **Deliver** — patched project (or configurator) + backups + report, stating clearly that nothing was compiled or run on hardware.

It comes from a real job: wiring the Unitree GO-M8010-6 motor driver into an STM32H723 + Keil + FreeRTOS project, then converting its receive path from blocking reads to DMA + idle-line detection. Every pitfall hit along the way is written down in `references/` and `examples/`.

### When to use it

**Good fit**

- You have a folder of `.c/.h` and an existing CubeMX/Keil/CMake/Make project to wire it into.
- You want a repeatable "install" tool for a library (GUI or CLI).
- You want to know exactly which files to touch, where, and how not to break the project.

**Not a fit**

- Creating a project from scratch (that is CubeMX's job).
- Modifying the HAL itself, or cross-compile verification (there is no toolchain here — only static checks).
- Licensing work for commercial/closed SDKs.

### Install

```powershell
git clone https://github.com/ZihanZhang114514/c-lib-configurator-skill.git
cd c-lib-configurator-skill
.\scripts\install-skill.ps1 -Target dsh        # into %USERPROFILE%\.dsh\skills\
.\scripts\install-skill.ps1 -Target all        # also into .claude\skills\
```

Restart the session (or open a new one in the Web GUI); `c-lib-configurator` should appear in the skill catalog.

Manual install works too: copy the whole folder to `%USERPROFILE%\.dsh\skills\c-lib-configurator\`; `SKILL.md` must sit at its root.

### Layout

```
c-lib-configurator/
├── SKILL.md                        # the skill: workflow, decision matrix, rules, pitfalls
├── README.md                       # this file (bilingual)
├── scripts/
│   ├── scan_project.py             # read-only scanner: build system, taken resources, callbacks, anchors
│   └── install-skill.ps1           # install into DSH / Claude Code
├── assets/
│   └── configurator_template.py    # configurator skeleton (GUI+CLI, backups, anchored inserts, .uvprojx)
├── references/
│   ├── patch-playbook.md           # newlines/BOM/anchors/.uvprojx/callbacks/DMA, with code
│   └── checklist.md                # tick-box lists for before-you-start and before-you-ship
└── examples/
    └── stm32-keil-uart-dma.md      # the real case study + six recorded pitfalls
```

### Quick start

**1 — Scan the project (read-only)**

```powershell
python scripts\scan_project.py "D:\proj"
python scripts\scan_project.py "D:\proj\MDK-ARM" --json
python scripts\scan_project.py "D:\proj" --grep "MyLib_"
```

It reports: build system, real project root, source dirs, include paths, used/free DMA streams, defined IRQ handlers, defined HAL callbacks, CubeMX usage, RTOS usage.

**2 — Configure directly, or generate a configurator**

- Directly: follow `SKILL.md`, touch 4–5 files (backup first).
- Configurator: copy `assets/configurator_template.py`, fill in `config.json`, edit only the two `TODO(project-specific)` spots.

**3 — Verify (without a cross-compiler)**

Sandbox copy → structural checks (braces/comments) → XML validation → grep the HAL symbols used → rerun four project shapes → confirm idempotency.

### Problems it answers

| Question | Answer it encodes |
|---|---|
| Which files do I touch? | Driver dir, existing HAL-callback file, `freertos.c`, `.uvprojx` (+ optional port layer) |
| How do I survive CubeMX regeneration? | Only insert inside `USER CODE` blocks; put the rest in new files |
| How do I make reruns safe? | Marker + normalised comparison + full backup |
| The project already integrates this library? | Scan for its prefix; if hit, sync files only, do not re-wire |
| Duplicate IRQ handlers / callbacks? | Check `*_IRQHandler` and `HAL_*Callback` first; forward a line instead of defining a second copy |
| No compiler to prove it? | Sandbox + structural checks + XML parse + HAL symbol grep |

### Pitfalls (all recorded under `references/`)

1. Mixed line endings + `find("\r\n")` → the patch landed at end-of-file.
2. UTF-8 BOM written by PowerShell → `ET.fromstring` raised `invalid token`.
3. Concatenating before `</Files>` → broken indentation, `</Files>` pushed to column 0.
4. Two integrations alive at once → duplicate `g_go_joint` symbol and two tasks fighting over one half-duplex bus.
5. Reusing `huart2` without correcting the baud rate → sends but never receives.
6. The demo task moved the motor on boot → nothing was expected to move; changed to zero-torque for 2 s first.

### Licence

No licence file means all rights reserved; the content is a working notebook and the ideas are free to reuse. The `Unitree_GO` driver itself lives in the `Unitree_GO_dm_lib` repository.
