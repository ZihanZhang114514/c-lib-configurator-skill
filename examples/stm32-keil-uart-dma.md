# 案例：宇树 GO-M8010-6 接进 STM32H723 + Keil 工程

这套流程第一次成形时的真实记录。数字和坑都是实测的，可以直接对照自己的情况。

---

## 背景

- 目标工程：RoboCon 2027 电控固件，`STM32H723VGTx`，CubeMX 生成 + Keil MDK（`R1.uvprojx`）+ FreeRTOS + CMSIS-RTOS v2；
- 要接的库：宇树 GO-M8010-6 关节电机驱动（半双工 RS485，4 Mbps，17/16 字节定长帧，CRC16-CCITT）；
- 板子：DM-MC-Board02，板载两路 RS485（USART2/PD5 PD6/DE PD4、USART3/PD8 PD9/DE PB14）；
- 需求变化：先是阻塞接收，后来要改成 **DMA + 空闲中断**。

## 第一次：摸工程（只读）

```powershell
python scan_project.py D:\27RC\dm_demo
```

关键输出：

```
keil device  : STM32H723VGTx   target(s): R1
source dirs  : bsp\uper(22) bsp\chassis(20) Core\Src(12) app(12) Core\Inc(10) app_lib(4)
uart handles : huart1, huart5                  <- 只有两路真正在用
dma streams  : DMA1_Stream0, DMA1_Stream1, DMA1_Stream2, DMA1_Stream6
dma free     : DMA1_Stream3, DMA1_Stream4, DMA1_Stream5, DMA1_Stream7, DMA2_*
irq handlers : 13  (DMA1_Stream0/1/2/6, FDCAN*, TIM*, UART5, ...)
hal callbacks: HAL_FDCAN_RxFifo0Callback, HAL_TIM_PeriodElapsedCallback,
               HAL_UARTEx_RxEventCallback, HAL_UART_ErrorCallback
cubemx       : ioc=['R1.ioc'], files with USER CODE=21
```

由此确定的四件事：

1. USART2 是空的 → 移植层自己建，4 Mbps；
2. DMA1 Stream3/4/5/7 空闲 → 给 USART2_RX 挑 `DMA1_Stream3`；
3. `HAL_UARTEx_RxEventCallback` 已被 SBUS 占用 → **不能**再定义，只能在 `app/usart_it.c` 里插一行转发；
4. `USER CODE` 锚点齐全 → 线程插入走 `freertos.c` 的 4 个锚点。

## 第二次：写配置器

产物：`Unitree_GO_dm_lib/`（驱动 + `config.json` + `Unitree_GO_configurator.py` + README）。

工具做的 5 件事（全部幂等、先备份）：

| # | 动作 | 目标 |
|---|---|---|
| 1 | 拷驱动、按通道改写 `UNITREE_GO_485_CHANNEL` | `bsp/uper/Unitree_GO.c/.h` |
| 2 | 生成串口 + DMA + 中断入口 + 回调 + 示例任务 | `bsp/uper/Unitree_GO_port.c/.h` |
| 3 | 在已有 HAL 回调里插一行转发 | `app/usart_it.c` |
| 4 | 插线程声明/属性/`osThreadNew` | `Core/Src/freertos.c` |
| 5 | 登记文件 + 补 `../bsp/uper` | `R1.uvprojx` |

## 第三次：把接收改成 DMA + 空闲中断

驱动侧变化：

```c
/* 旧 */ uint8_t Unitree_GO_SendRecv(...);   // 发 + 阻塞 HAL_UART_Receive(16B, 1ms)
/* 新 */ uint8_t Unitree_GO_Send(...);       // 只发，发完立刻 StartRx()
         uint8_t Unitree_GO_Poll(...);       // 读 rx_seq，不阻塞
         uint8_t Unitree_GO_SendRecv(...);   // Send + 最多等 3ms 的 Poll
         void    Unitree_GO_StartRx/StopRx(...);
         void    Unitree_GO_RxEvent(huart, size);   // IDLE 转发入口
         void    Unitree_GO_Error(huart);
```

时序（半双工）：

```
StopRx() → DE 高 → 阻塞发 17 B（34 µs）→ DE 低 → StartRx()
→ 电机回 16 B → 总线空闲 1 字符（≈2.5 µs）→ IDLE 中断
→ RxEvent：找 0xFD 0xEE → 按帧内 ID 分发 → ParseData → 重新武装
```

实测要点：

- DMA 缓冲给 **32 字节**（不是 16）：容一帧之后的回波/残包，找帧头时只认 `0xFD 0xEE` 起始的 16 字节；
- 关掉**半传输中断**（`__HAL_DMA_DISABLE_IT(hdmarx, DMA_IT_HT)`）：16 字节的帧用不着 HT，少一次中断；
- `SendRecv` 等待时间 **3 ms**：4 Mbps 一帧 32 µs，电机处理 + 总线占用留足余量；
- 没配 DMA 时（`huart->hdmarx == NULL`）自动退回阻塞接收，老工程不会"编译过但收不到"。

## 踩过的坑（按代价排序）

### 1. 两份接入代码，符号直接撞车

工程里原本有一份手写的 `APP_Unitree_test()`，定义了全局 `g_go_joint`。配置器第二次运行时如果无脑生成移植层，`bsp/uper/Unitree_GO_port.c` 里也会定义 `g_go_joint` → **链接期重复符号**，而且两个任务会抢同一路半双工总线。

修法：扫描非库目录里是否出现库前缀（`Unitree_GO_`、`APP_Unitree_test`、`g_go_joint`），命中就只同步驱动文件、不生成移植层，并在日志里说清楚。

### 2. 混合换行把代码插到了文件末尾

测试夹具里 `freertos.c` 前几行是 LF、末行是 CRLF（PowerShell `Set-Content` 的杰作）。`text.find("\r\n", idx)` 找不到锚点行的行尾，一路找到 EOF —— 补丁"成功"了，但 `osThreadNew` 掉在文件最后一行。

修法：**找行尾永远用 `"\n"`**（见 `references/patch-playbook.md` §2）；换行符只用来决定"插入的片段用哪种换行"。

### 3. BOM 让 `ET.fromstring` 报 invalid token

PowerShell 写的文件带 UTF-8 BOM，`read_text(encoding="utf-8")` 把 BOM 当成正文，XML 解析直接炸。

修法：读文件用 `utf-8-sig`。

### 4. `</Files>` 前插入导致缩进错位

第一版把 `<File>` 块拼在 `</Files>` 前面，结果 `<File>` 继承了 `</Files>` 的 10 个空格再叠加 12 个 → 22 个空格；`</Files>` 掉到行首。XML 合法、Keil 能读，但 diff 很脏。

修法：插到 `text.rfind("\n", 0, files_close) + 1`（行首）。

### 5. 复用句柄 ≠ 参数正确

`huart2` 存在、`MX_USART2_UART_Init` 也存在，但波特率是 115200。只判断"句柄存在"就复用，结果是"能发不能收"。

修法：复用分支里校正波特率，改完再 `HAL_UART_Init`；不改引脚（信任工程）。

### 6. "示例任务"默认就会让电机转

第一次安装完成、Rebuild、上电，电机直接往 0.5 rad 走 —— 用户没预期。

修法：示例任务先 **零力矩 2 s**（手拨能轻松转 = 通信 + FOC 都正常），再回零、再位置伺服；README 和日志里明确写"这会真的动电机"，并给 `--no-task`。

## 数字小结

| 项 | 值 |
|---|---|
| 工程规模 | 80 个 `.c/.h`（不含 Drivers/Middlewares），9 个 Keil 组 |
| 生成/修改文件 | 4 个 `.c/.h` + 1 个 `.uvprojx` |
| 一次配置耗时 | < 1 s |
| 重跑结果 | 全部 `[不变]`，零改动 |
| 驱动代码量 | `.c` 约 800 行，`.h` 约 280 行 |
| 支持的形态 | 全新工程 / 已装过 / 已有同类接入 / 通道 1 / 通道 2 |
