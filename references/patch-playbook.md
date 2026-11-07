# 补丁手册 / patch playbook

把库接进工程时，90% 的事故来自"改文件的姿势"，不是业务代码。这里是可以直接抄的实现细节。

---

## 1. 文件读写：换行、BOM、编码

```python
def read_text(path):
    # utf-8-sig：BOM 会被吃掉。带 BOM 的 uvprojx 交给 ET.fromstring 会报
    # "invalid token"，信息量和"打开方式不对"差不多。
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()

def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:   # newline='' 才会原样写出
        f.write(text)

def nl_of(text):
    """文件的主导换行符。不要写 'if "\r\n" in text'：混合换行时那是错的。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"
```

- `newline=""` 是必须的：默认的 universal newlines 会把 CRLF 读成 LF，写回时整份文件的换行都变了，git diff 一片红。
- 编码：嵌入式工程里 GB18030 的注释很常见。读之前可以先试 `utf-8-sig → gb18030 → latin-1`（latin-1 永不失败，兜底）。
- 只在文本里改，别做二进制 patch。

---

## 2. 锚点插入

**锚点要"结构化且稳定"**：`/* USER CODE BEGIN xxx */`、函数体第一个 `{` 之后、最后一个 `#include` 之后。

```python
def insert_after_line(text, anchor, snippet):
    idx = text.find(anchor)
    if idx < 0:
        return None
    line_end = text.find("\n", idx)      # <-- 一定是 "\n"
    if line_end < 0:
        return None
    pos = line_end + 1
    return text[:pos] + snippet + nl_of(text) + text[pos:]
```

为什么不能用 `text.find("\r\n", idx)`：只要文件里前几行是 LF、末尾一行是 CRLF（PowerShell `Set-Content` 就会造出这种文件），`find` 就一路找到文件末尾 —— 片段被插到最后一行，看起来像"改成功了"，其实函数体已经乱套。

**不要用**：行号（上一个人加两行注释就全错）、`第 N 次出现`（注释里也可能出现）、大段整文件替换（冲突后无法定位）。

插入函数体内部（比如回调开头）：

```python
m = re.search(rf"void\s+{fn}\s*\([^)]*\)\s*\{{", text)   # 只匹配函数头 + 开括号
text = text[:m.end()] + snippet + text[m.end():]
```

注意 `[^)]*` 不能跨行换行符以外的东西；参数列表里有换行时用 `re.S` + `[^)]*`。

---

## 3. 幂等：marker + 归一化比较

```python
MARKER = "MYLIB_AUTOCONFIG"

def patch_once(text, anchor, snippet):
    if MARKER in text:                     # 已经插过
        return text, "unchanged"
    ...
```

- marker 要**唯一且带库名前缀**，避免和工程的注释撞；
- 生成文件（整份覆盖的那种）在文件头写"本文件由工具生成，重跑会整份覆盖"，并把"生成时间"行在比较时归一化，否则每次运行都被判成"有变化"：

```python
def stable(text):
    return re.sub(r"^.*生成时间.*$\r?\n?", "", text, flags=re.M)
```

---

## 4. `.uvprojx` 编辑（Keil MDK）

结构（简化）：

```xml
<Project>
  <Targets><Target><TargetName>R1</TargetName>
    <Groups>
      <Group><GroupName>bsp_upper</GroupName><Files><File>…</File></Files></Group>
      <Group><GroupName>::CMSIS</GroupName></Group>
    </Groups>
    <TargetOption><TargetArmAds><Cads><VariousControls>
      <IncludePath>../Core/Inc;../bsp/uper</IncludePath>
    </VariousControls></Cads></TargetArmAds></TargetOption>
  </Target></Targets>
</Project>
```

插入 `File`：

```python
entry = (f"            <File>{nl}"
         f"              <FileName>{name}</FileName>{nl}"
         f"              <FileType>1</FileType>{nl}"       # 1 = C
         f"              <FilePath>{fpath}</FilePath>{nl}"
         f"            </File>{nl}")
files_close = text.find("</Files>", g_start, g_end)
line_start = text.rfind("\n", 0, files_close) + 1          # 行首，不是标签前
text = text[:line_start] + entry + text[line_start:]
```

**必须遵守的四条**：

1. 往**行首**插，不要把 `entry` 直接拼在 `</Files>` 前面 —— 那会把 10 个空格的缩进吃掉，`<File>` 和 `</Files>` 全歪，Keil 仍能读，但 diff 难看且容易手滑；
2. 路径用**反斜杠 + `..\` 相对 MDK-ARM**（`..\bsp\uper\xxx.c`），和同组其它条目的风格一致；
3. 同名文件已在任一 target 的组里存在就跳过（多 target 工程要**逐组**判断，别全局判断一次）；
4. 写完立刻 `ET.fromstring(text.encode("utf-8"))`；抛异常就**不要写文件**。

`IncludePath` 只补**主 target**（含 `../Core/Inc` 的那些 Cads），追加 `;../bsp/uper`，去重：

```python
def add_include(text, entry):
    def repl(m):
        value = m.group(2).strip()
        if not value or "../Core/Inc" not in value:
            return m.group(0)
        parts = [p.strip() for p in value.split(";") if p.strip()]
        return m.group(0) if entry in parts else f"{m.group(1)}{value};{entry}{m.group(3)}"
    return re.sub(r"(<IncludePath>)([^<]*)(</IncludePath>)", repl, text)
```

CMake 工程同理：`target_sources()` / `target_include_directories()`，插在已有同类型语句旁边。

---

## 5. HAL 回调：转发，不要抢

`HAL_UARTEx_RxEventCallback` / `HAL_UART_ErrorCallback` 是**全局独占**的弱符号：工程里已经实现了一个（SBUS、陀螺仪都靠它），你再定义一份就是重复定义；不定义，你的驱动永远收不到 IDLE。

正确姿势 —— 检测到已有定义就**插一行转发**，没有才自己定义：

```c
void HAL_UARTEx_RxEventCallback(UART_HandleTypeDef *huart, uint16_t Size)
{
    /* MYLIB_AUTOCONFIG: 库的接收分发 */
    MyLib_RxEvent(huart, Size);      /* 只认自己那一路，其它串口原样返回 */
    ... 原来的 SBUS / 陀螺仪处理 ...
}
```

驱动侧用「按 `huart` 找句柄」的分发函数，天然对多串口安全；同一个总线上挂多个设备时，再按帧里的 ID 二次分发。

---

## 6. DMA stream 选择与中断入口

STM32H7 有 DMAMUX：**任何空闲 stream 都能接任何 request**（`DMA_REQUEST_USART2_RX = 43`），所以"选 stream"= "挑一个没人用、且中断函数没被定义的"。

```python
used   = set(re.findall(r"Instance\s*=\s*(DMA[12]_Stream\d+)", blob))
defined= set(re.findall(r"void\s+(DMA[12]_Stream\d+_IRQHandler)\s*\(", blob))
free   = [f"DMA1_Stream{i}" for i in range(8) if f"DMA1_Stream{i}" not in used
          and f"DMA1_Stream{i}_IRQHandler" not in defined]
```

- 中断入口（`USARTx_IRQHandler`、`DMAx_StreamN_IRQHandler`）**只在工程里没有时生成**，否则链接报重复；
- 生成时把这段注释写上："以后 CubeMX 也启用这路，就在 `stm32h7xx_it.c` 生成同名函数，届时删掉这里"；
- `__HAL_LINKDMA(&huart, hdmarx, hdma)` 必须在任何 `HAL_UARTEx_ReceiveToIdle_DMA()` 之前。

---

## 7. 复用 vs 生成（串口）

```python
declared = re.search(rf"UART_HandleTypeDef\s+{uart};", usart_c + usart_h)
inited   = re.search(rf"void\s+MX_{inst}_UART_Init\s*\(", ...)
baud     = re.search(rf"{uart}\.Init\.BaudRate\s*=\s*(\d+)", ...)
```

- `declared` → 复用，只校正参数：

```c
if ((huart2.Instance == USART2) && (huart2.Init.BaudRate != 4000000u)) {
    huart2.Init.BaudRate = 4000000u;
    (void)HAL_UART_Init(&huart2);
}
```

- 否则自己建：时钟选择（H7 的 `RCC_PERIPHCLK_USART2` + `RCC_USART234578CLKSOURCE_D2PCLK1`）、GPIO AF、`HAL_UART_Init`、NVIC 一条龙；
- **扫描要剥注释**：`//extern UART_HandleTypeDef huart2;` 这种被注释掉的声明会让"复用"判断误判成 true。

---

## 8. 备份、回滚、报告

- 备份根目录：`<MDK-ARM>/<LIB>_backup/<yyyyMMdd_HHmmss>/`，**保留相对路径**；
- 备份根目录本身要排除在扫描范围外（否则重跑时它会被当成源码扫进去，或者被再备份一次）；
- 写失败要能回滚：先写同目录 `.tmp` 再 `os.replace`，或写前记下原文，异常时写回；
- 报告里逐条列出 `[written] / [patched] / [unchanged]`、备份路径、下一步（Rebuild、确认 ID、看哪个计数器）。

---

## 9. 没有编译器时的结构检查

```python
def strip_comments(src):
    """把注释和字符串换成空格，保留其它字符；用于括号平衡与关键字扫描。"""
    ...
# 检查 (){}[] 平衡；检查"块注释里出现 /*"（会导致提前闭合）
```

另外把生成代码里用到的每个 HAL 符号在 `Drivers/` 里 grep 一遍，确认这版 HAL 真的有它（例如 `HAL_UARTEx_ReceiveToIdle_DMA` 在 `stm32h7xx_hal_uart_ex.c`，不在 `hal_uart.c`）。

---

## 10. 反模式

| 反模式 | 后果 |
|---|---|
| 直接改 `MX_USARTx_UART_Init` / `HAL_UART_MspInit` | CubeMX 重新生成就丢，且每次生成都要重改 |
| 用行号定位插入点 | 上一个人加两行注释，补丁插进函数体中间 |
| 不查 marker 就插 | 重跑一次，`osThreadNew` 出现两遍，任务翻倍 |
| 全局判断"文件已在工程里" | 多 target 工程只登记了第一个 target |
| 生成的中断函数不看工程已有定义 | 链接期重复符号，报错信息指向 `startup_*.s`，很难查 |
| 不备份直接改 | 用户手改的注释没了，无法回滚 |
| 声称"编译通过"但没编译 | 用户 Rebuild 后一堆未定义符号，信任崩塌 |
