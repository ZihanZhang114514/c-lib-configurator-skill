---
name: c-lib-configurator
description: Wire a C library (a folder of .c/.h, maybe a config) into an existing embedded C project — Keil MDK (.uvprojx), CMake or Make — or build the Python one-click configurator that does it. Covers probing the project first (source dirs, include paths, which UART/DMA/IRQ/HAL callbacks are already taken), deciding reuse-vs-generate for peripherals, generating a port layer instead of editing CubeMX output, anchor-based idempotent patching with mandatory backups, .uvprojx XML edits that stay well-formed, and a verification loop that works when no cross-compiler is installed.
whenToUse: The user points at a directory that holds a C library's files and asks to configure/port/install it into a project, or asks for a Python/GUI "one-click configurator" (自动配库器) for that library.
---

# C 库自动配置器 / wiring a C library into an embedded project

给一个放库文件的目录（`.c/.h`，可能带 `config`），把它接进已有 C 工程；或者把这套接法写成可复用的 Python 一键配置器。

**先读库，再读工程，最后才动手改文件。** 顺序颠倒的代价在文末的"踩过的坑"里。

---

## 0. 先分清三种任务

| 任务 | 用户说的话 | 交付 |
|---|---|---|
| **A. 直接配置** | "把这个库配到我的工程里" | 改好的工程 + 备份 + 一段说明 |
| **B. 写配置器** | "写个脚本/工具，输入路径就能装" | `configurator.py` + `config.json` + README |
| **C. 先 B 再 A** | "既能一键配，也帮我配好" | 先写工具，再用工具配工程（并用工具的输出当验收） |

判断依据：**这件事以后还会不会重复做？** 会 → B；只做一次 → A。判断不了就先 A 跑通一遍，再抽成 B（A 跑通的过程中自然会发现所有坑）。

---

## 1. 摸清库（不要跳过）

1. `ls` 库目录，逐个读头文件 —— API 面、宏配置、依赖（HAL / RTOS / 第三方）。
2. 判断**分层**：协议层（与板子无关，可原样复用）vs 移植层（引脚、串口、DMA、任务）。库如果只有协议层，移植层就得生成。
3. 列出**它需要的资源**：外设实例、DMA 通道、中断、HAL 回调、RTOS 线程、定时器、GPIO。
4. 找**已有的同类封装**（同工程里 RS485/CAN 电机驱动、陀螺仪驱动）—— 新驱动的接口命名、目录位置、注释风格都对齐它们，用户会更好维护。

产出一张"库需要什么"的表，后面每一条都要在工程里落实：

| 库需要 | 例子 | 谁提供 |
|---|---|---|
| 串口句柄 4 Mbps | `huart2` | 工程已有则复用，否则移植层建 |
| DMA RX | `USART2_RX` + 一个空闲 stream | 同上 |
| NVIC | USART2 + DMA stream | 生成时别和已有中断函数重名 |
| HAL 回调转发 | `HAL_UARTEx_RxEventCallback` 里一行 | 工程已有则插入，没有则定义 |
| RTOS 线程 | `osThreadNew` | 插到 `USER CODE` 区 |

## 2. 摸清工程 —— 先跑扫描器，别手查

```powershell
python <skill-dir>\scripts\scan_project.py "D:\proj"                 # 人类可读
python <skill-dir>\scripts\scan_project.py "D:\proj\MDK-ARM" --json   # 机器可读
python <skill-dir>\scripts\scan_project.py "D:\proj" --grep "MyLib_"  # 是否已接过
```

只读、不改任何文件。它回答动手前必须确认的事：

- 构建系统（`.uvprojx` / `CMakeLists.txt` / `Makefile`）、真实的工程根目录（`MDK-ARM` 的上一级）、MCU 型号；
- 源目录分布、`IncludePath` 列表（Keil 的 `<IncludePath>` 或 CMake 的 `include_directories`）；
- 已被占用的 **UART 句柄 / 实例 / 初始化函数**；
- 已被占用的 **DMA stream 与 DMA request**，以及**空闲 stream 列表**；
- 已定义的 **`*_IRQHandler`**（生成中断入口前必须查，否则链接报重复符号）；
- 已定义的 **HAL 回调**（决定"插一行转发"还是"自己定义"）；
- 是否 CubeMX 生成（`.ioc` + 文件里 `USER CODE` 锚点数量）；
- 是否 RTOS（`cmsis_os.h`、`osThreadNew` 次数、`freertos.c`）。

扫描结果里最容易被忽略、代价最大的一条是**"工程里已经有人接过这个库"**：两份接入代码同时跑，轻则抢总线，重则符号重复、整机 HardFault。`--grep` 要拿库的前缀（`MyLib_`、`APP_MyLib`）去扫。

## 3. 决策矩阵

| 情况 | 做法 |
|---|---|
| 工程已有该库的接入代码 | **只同步库文件**，不加任务、不重复接中断；提示用户先删旧的 |
| 工程已有目标外设（`huart2` / `MX_USART2_UART_Init`） | **复用**：不重建，只在初始化时校正参数（如波特率改成 4 Mbps） |
| 工程已有该外设的 DMA | 复用；`__HAL_LINKDMA` 已在 CubeMX 的 MSP 里做过 |
| 都没有 | **生成移植层**：`port.c/.h` 自己建串口 + DMA + NVIC + 中断入口 |
| 目标文件是 CubeMX 生成的 | 只往 `/* USER CODE BEGIN/END */` 里插；新逻辑放新文件 |
| 外设初始化在 `SystemClock_Config` 之后但任务之前 | 生成幂等的 `Xxx_Port_Init()`，串口只在第一次调用时初始化 |

**一条铁律：不要改 CubeMX 生成的结构性代码**（`MX_USARTx_UART_Init`、`HAL_UART_MspInit`、`stm32h7xx_it.c` 里的中断分发）。要补的东西一律走"新文件 + USER CODE 区插入"。

## 4. 移植层契约

生成的 `xxx_port.h/.c` 至少要有：

```c
void      xxx_Port_Init(void);                 /* 幂等：static uint8_t inited */
void      APP_xxx_Task(void *argument);        /* 可选的示例任务 */
extern    xxx_HandleTypeDef g_xxx;             /* 给调试器/其它模块 */
#define   XXX_MOTOR_ID  1u                   /* 用户可以手改的参数 */
```

- `_Port_Init()` 内部做：串口时钟/引脚/参数 → DMA（`HAL_DMA_Init` + `__HAL_LINKDMA`）→ NVIC → 库的 `Init()`；
- 中断入口（`USART2_IRQHandler`、`DMA1_StreamN_IRQHandler`）**只在工程里没有同名函数时**生成，并加注释说明"以后 CubeMX 也开了这路，就删掉这里"；
- HAL 回调（`HAL_UARTEx_RxEventCallback` 等）**只在工程里没有定义时**生成，否则改为在工程已有的回调里插一行转发 —— 因为多个外设共用同一对回调。

## 5. 补丁四原则

1. **锚点**：插在 `USER CODE` 标记、`#include` 之后、函数体开头这类稳定位置；不要按行号、不要按"第 N 个 xxx"。
2. **幂等**：每个插入片段带唯一 marker（如 `/* MYLIB_AUTOCONFIG */`），重跑先查 marker；文件内容相同就跳过（比较时把时间戳行归一化）。
3. **备份**：写之前 `shutil.copy2` 到 `MDK-ARM/<LIB>_backup/<时间戳>/`，保留相对路径，写报告。
4. **校验**：`.uvprojx` 写完先 `ET.fromstring()` 解析，失败就回滚；C 文件做括号/注释平衡检查。

四个**真踩过的**坑（详见 `references/patch-playbook.md`）：

| 坑 | 现象 | 修法 |
|---|---|---|
| 换行符混用 | 片段插到了文件末尾 | 找行尾用 `"\n"`，不要用 `"\r\n"` |
| UTF-8 BOM | `ET.fromstring` 报 `invalid token` | 读文件用 `utf-8-sig` |
| `</Files>` 前插入 | `<File>` 缩进错位、`</Files>` 掉到行首 | 插到 `rfind("\n", 0, pos) + 1`（行首） |
| 重复接入 | 链接报符号重复 / 两个任务抢总线 | 扫描 `--grep` + marker 检测，命中就只同步不接线 |

## 6. 写配置器：直接改模板，不要从零写

`assets/configurator_template.py` 是跑通过的骨架（GUI + CLI + config.json + 备份 + 锚点插入 + uvprojx 登记 + dry-run + 报告）。复制到库目录，只改两处：

- `config.json`：库文件 → 目标路径、插入点、uvprojx 组名/包含路径；
- `TODO(project-specific)`：外设 bring-up（生成 port 层）、重复接入检测、回调转发。

最低要求（用户会按这个判断"能用"）：

```powershell
python configurator.py                      # GUI
python configurator.py --project PATH       # CLI
python configurator.py --project PATH --dry-run   # 只报告，不写
```

GUI 只需要：路径输入 + 浏览 + 一键安装 + 日志框；参数（通道/ID/周期）放进选项区，默认值写 `config.json`。

## 7. 没有交叉编译器时怎么验证

这类环境通常没有 armcc/arm-none-eabi-gcc，**不要谎称"编译通过"**。按顺序做：

1. **沙箱副本**：`robocopy 工程 副本 /E /XD Drivers Middlewares`，在副本上跑配置器，别动用户真实工程；
2. **结构检查**：注释状态机剥掉注释后数 `(){}[]`；确认没有"块注释里出现 `/*` 导致的提前闭合"；
3. **XML 校验**：`ET.parse(uvprojx)`；再人眼确认 `<File>` 落在正确组、缩进对齐；
4. **符号核对**：把生成代码里用到的 HAL 宏/函数（`HAL_UARTEx_ReceiveToIdle_DMA`、`DMA_REQUEST_USART2_RX`、`__HAL_LINKDMA`…）逐个在 `Drivers/` 里 grep 到定义；
5. **多形态回归**：全新工程 / 已装过一次 / 已有同类接入 / 另一路通道，四种都要跑；
6. **幂等复跑**：第二次运行应当全部 `[same]`，零改动。

## 8. 常见错误清单

| 现象 | 原因 |
|---|---|
| 生成的中断函数和工程里的重名 | 没查 `*_IRQHandler` 就生成 |
| 回调被定义两次 | 工程已有 `HAL_UARTEx_RxEventCallback`，又在 port.c 里定义 |
| 复用了句柄但波特率不对 | 只判断了"句柄存在"，没校正参数 |
| 装了库但收不到数据 | 回调没转发 / NVIC 中断没开 / DMA 没 link |
| 重跑把用户手改的代码冲掉 | 生成文件没加"整份覆盖"警告，或补丁没查 marker |
| 备份目录把备份又备份进去 | 备份根目录放在扫描范围内且没排除 |

## 9. 交付物

- 库 + 配置器 + `config.json` + README（API、配置、排错、限制）；
- 一次真实的 sandbox 运行日志 + 报告文件；
- 明确写清**没做**的事（没编译、没上机、需要用户确认的 ID/引脚）。

---

## 参考

- `references/patch-playbook.md` — 锚点插入、幂等、uvprojx 编辑、回调转发、DMA stream 选择的全部细节。
- `references/checklist.md` — 动手前/交付前逐条打勾。
- `examples/stm32-keil-uart-dma.md` — 真实案例：宇树 GO-M8010-6 接进 STM32H723 + Keil 工程（含踩坑记录与数字）。
- `assets/configurator_template.py` — 配置器骨架。
- `scripts/scan_project.py` — 工程扫描器。
