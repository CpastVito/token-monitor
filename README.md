# WorkBuddy Token 监测

对 **WorkBuddy / Qoder / Codex / Claude** 等桌面 AI 应用的模型流量做**只读**监测：**上下行 tokens/秒**、输入 / 输出 tokens、缓存命中、积分消耗、上下文占用。
设计参考 [ccusage](https://ccusage.com) 的燃烧率与状态条语义、[splitrail](https://github.com/Piebald-AI/splitrail) 的环比 / 堆叠趋势 / 可筛选明细结构。

> **English** · A local-first, read-only usage monitor for desktop AI apps — WorkBuddy, Qoder, Codex and Claude. Live up/down tokens per second, cache hit rate, credit burn and context usage in a floating capsule, a full dashboard and a desktop pet. Optional balance checks cover DeepSeek, OpenRouter, Moonshot, New API, linoroute, LMU and Zhipu GLM. Built with Python + tkinter for Windows; usage data stays on your machine and only the balance requests you enable reach your providers.
>
> **Keywords:** LLM token monitor · AI usage tracker · cost tracking · desktop pet · tkinter dashboard · local-first

| 形态 | 说明 |
|---|---|
| **置顶悬浮胶囊** | 无边框浮窗，可拖到模型选择器旁边，实时显示 `模型 · 速率 ↓下行 ↑上行 t/s · 上下文%` |
| **完整仪表盘** | 本地网页：暗色速率状态条 + 环比 KPI + 堆叠趋势 + 生成速率基线 + 会话明细 |

---

## 一、先说清楚一件事：为什么不是"直接嵌进输入框"

**WorkBuddy 桌面端没有可供第三方注入的状态栏或 UI 扩展点。** 实测依据：

- `app.asar` 中出现的 8 处 `statusLine` 全部是 Electron 网络层里的 **HTTP 响应状态行**（`statusLine: "HTTP/1.1 204 No Content"`），与界面状态栏无关；
- 模型选择器所在的输入卡片是打包后的 React 渲染层，没有官方 DOM 钩子。

所以本方案**不修改 WorkBuddy 任何文件**（不改 `app.asar`、不改 `settings.json`），而是读取它自己写下的用量记录，用一个置顶浮窗贴在模型选择器旁边 —— 视觉上是"嵌在边上"，工程上零侵入、不随版本更新失效。

---

## 二、速率怎么算（本版核心）

"每秒多少 token"没有现成字段，需要自己推导。本工具提供**两套口径**，并在界面上明确区分：

### 1. 实时速率（默认，胶囊与状态条使用）

| 项 | 算法 |
|---|---|
| 采样 | 同一会话内**相邻两次请求**的时间间隔 Δt（上一次结束 → 本次结束） |
| 下行 t/s | `completion_tokens ÷ Δt`（输出 / 解码） |
| 上行 t/s | `prompt_tokens ÷ Δt`（输入 / 预填充） |
| 窗口 | 最近 120 秒；无新请求时回落到最近 30 次并标注"空闲" |
| 过滤 | Δt ≤ 0 或 > 15 分钟视为异常/离开，剔除 |

**口径提示**：Δt 包含模型排队与工具执行时间，因此这是**端到端吞吐**，天然低于纯解码速度。

### 2. 生成速率基线（`traces` 提供真实耗时）

`~/.workbuddy/traces/**/trace_*.json` 中 `type == "generation"` 的 span 带 `duration`（毫秒），
按 `(sessionId, endedAt)` 与请求记录对齐（实测中位误差 **17ms**，命中率 80.9%）：

| 项 | 算法 |
|---|---|
| 下行 t/s | `completion_tokens ÷ span.duration` ← 接近模型真实解码速度 |
| 上行 t/s | `prompt_tokens ÷ span.duration` ← 预填充吞吐 |
| 统计 | 平均值、p50 / p95 耗时、覆盖请求数 |

**关键限制**：WorkBuddy 的 trace 在**会话/工作流结束后**才落盘（实测当天运行中的会话 `traces` 目录无新文件）。
因此基线是**历史参考值**，不是实时读数 —— 这也是为什么实时值必须走第 1 套口径。

本机实测基线（2026-10-20，1,545 次采样）：

| 模型 | 下行 t/s | 上行 t/s | 平均生成耗时 |
|---|---|---|---|
| DeepSeek-V4.1-Flash | 108.6 | 18.2K | 4.9 s |
| DeepSeek-V4-Pro | 95.1 | 4.7K | 18.6 s |
| Space-Bunny | 58.9 | 8.5K | 8.5 s |
| GLM-5.3 | 53.1 | 5.7K | 18.4 s |
| Kimi-K3-1 | 28.4 | 3.1K | 25.0 s |

---

## 三、数据来源

| 来源 | 字段 | 用途 |
|---|---|---|
| `workbuddy.db` → `sessions` | `model`、`context_window`、`updated_at` | 会话元数据 |
| `workbuddy.db` → `session_usage` | `used`、`size` | **实时上下文占用** |
| `projects/**/*.jsonl` → `providerData.rawUsage` | `prompt_tokens`、`completion_tokens`、`prompt_cache_hit_tokens`、`credit` | **逐请求 token 与积分** |
| `traces/**/trace_*.json` → `spans[generation]` | `duration`、`endedAt` | **真实生成耗时 → 基线速率** |

- 数据库以 `mode=ro` 打开，全程不写入；
- JSONL 与 trace 均按 `(mtime, size)` 指纹增量解析（首跑约 3 s，之后约 0.9 s）；
- 同一 `messageId` 只计一次，避免流式落盘重复计数。

### 其他指标口径

| 指标 | 定义 |
|---|---|
| 输入 / 输出 tokens | `prompt_tokens` / `completion_tokens` 累加（输入含缓存命中部分） |
| 缓存命中率 | `prompt_cache_hit_tokens ÷ prompt_tokens` |
| 积分消耗 | `rawUsage.credit` 累加（WorkBuddy 计费口径） |
| 燃烧率 | 区间内 tokens ÷ 活跃分钟（各会话首尾跨度之和，不含会话间空档） |
| 环比 | 与上一等长周期对比（今日 ↔ 昨日同一时刻；7 天 ↔ 前 7 天） |
| 上下文占用 | `session_usage.used ÷ size`，用于判断何时该新开会话 |

---

## 四、快速开始

双击 **`start.cmd`**：启动本地服务 → 启动悬浮胶囊 → 打开仪表盘。
停止：**`stop.cmd`**，或右键胶囊选「退出」。

### 胶囊交互

| 操作 | 效果 |
|---|---|
| 左键拖动 | 移动位置（自动记忆） |
| 双击 | 打开完整仪表盘（服务未启动会自动拉起） |
| 右键 → 改用生成速率基线 | 在「实时端到端」与「trace 真实解码速度」之间切换 |
| 右键 → 显示今日累计 | 追加 `今日 ↑… ↓…` 分段 |
| 右键 → 贴靠 / 居中 / 透明度 | 位置与外观调整 |

胶囊内容示例：

```
● DeepSeek-V4.1-Flash │ 速率 ↓  113 ↑ 6.2K t/s │ ▬▬▬ 23.7%
```

- ↓ 下行（输出）、↑ 上行（输入），单位 t/s；
- 数字为**定宽等宽字体**，避免数值跳动引起胶囊宽度抖动；
- 圆点：绿色＝2 分钟内有请求，灰色＝空闲；速率变暗表示当前无实时采样；
- 进度条 + 百分比：上下文占用，<50% 绿 / 50–80% 橙 / >80% 红。

### 手动运行

```bash
python server.py                    # 仪表盘服务 127.0.0.1:8971（端口占用自动顺延）
pythonw overlay.py                  # 胶囊，自动贴靠 WorkBuddy 窗口
pythonw overlay.py --no-dock        # 不贴靠，用上次位置
python collector.py today           # 单行文本（可接入其它状态栏）
```

---

## 五、仪表盘读数说明

| 区域 | 内容 |
|---|---|
| **暗色状态条** | 下行 / 上行 t/s、请求间隔中位、燃烧率、上下文、最近 60 分钟输出柱状图 |
| **KPI** | 输入 / 输出 / 请求 / 积分 / 缓存命中率，每项带环比 chip（▲ 增 / ▼ 减） |
| **堆叠趋势** | 按模型堆叠，可切「总量 / 仅输入 / 仅输出」，前 5 名固定配色 |
| **生成速率基线** | 逐模型真实解码速度、平均与 p95 生成耗时 |
| **会话明细** | 可搜索、点表头排序；含下行 t/s、时长、上下文占用 |

区间：`?scope=today|7d|30d|all`，也写入 URL hash 便于收藏。

---

## 六、文件说明

| 文件 | 作用 |
|---|---|
| `collector.py` | 数据层：DB + JSONL + trace 解析、增量缓存、速率与聚合 |
| `server.py` | 本地 HTTP 服务：`/`、`/api/summary?scope=`、`/api/live`、`/api/health` |
| `dashboard.html` | 仪表盘（单文件、手写 SVG、无外部依赖、可离线） |
| `overlay.py` | 置顶桌宠与数据面板（tkinter，无第三方依赖） |
| `uitheme.py` | 跨端视觉令牌：颜色与字阶与 `dashboard.html` 共用一套 |
| `petmenu.py` | 自绘右键菜单（Canvas 画圆角面板，不再用系统菜单画法） |
| `start.cmd` / `stop.cmd` | 一键启停（`stop.ps1` 按命令行精确匹配本目录进程） |
| `overlay-state.json` | 胶囊位置 / 速率口径 / 显示选项（自动生成） |
| `.cache/usage-cache.json` | 解析缓存（自动生成，可安全删除） |

---

## 七、常见问题

**1. 胶囊没出现，日志提示 `No module named 'tkinter'`**
WorkBuddy 自带托管 Python 未包含 tkinter。`overlay.py` 会自动切换到系统 Python
（`%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe`）重启自身。

**2. 速率显示"—"或变暗**
说明最近 120 秒没有新请求（空闲），此时回落展示最近 30 次的值并置灰。
发起一次对话即可刷新。

**3. 基线表为空**
`traces` 目录只有在会话/工作流结束后才写入；新会话需要等结束（或重启 WorkBuddy）后才有基线数据。

**4. 胶囊位置偏了**
显示器非 100% 缩放时坐标需按 DPI 换算，`overlay.py` 已用 `GetDpiForSystem()` 处理。
窗口移动后右键「贴靠 WorkBuddy 窗口」重算，或直接拖动（位置会被记住）。

**5. 启动第二个胶囊没反应**
单实例保护：胶囊占用 `127.0.0.1:8799` 作为互斥锁，重复启动会直接退出。

**6. 想开机自启**
把 `start.cmd` 快捷方式放进 `shell:startup`，或在任务计划程序里设为登录触发。

---

## 八、本机全量基线（2026-10-20）

| 项目 | 数值 |
|---|---|
| 请求数 | 6,749（含子 Agent 394） |
| 输入 tokens | 871.6M |
| 输出 tokens | 5.8M |
| 缓存命中率 | 95.8% |
| 积分消耗 | 9,168.78 |
| 实时端到端速率 | 下行 ≈31 t/s、上行 ≈7.3K t/s |
| 生成基线速率 | 下行 54.4 t/s、上行 7.1K t/s |
| 覆盖模型 | 15 个，前三位：DeepSeek-V4.1-Flash 77.3%、DeepSeek-V4-Pro 9.9%、Kimi-K3-1 4.4% |

> 缓存命中率长期高于 95%：多轮 Agent 调用中上下文被大量复用，这解释了"输入 tokens 极大但积分不高"。
> 实时速率（含工具执行）约为生成基线的 1/2～1/3，可用于判断当前对话的"体感快慢"。

---

## 九、桌宠模式（v3）

胶囊已升级为**桌宠**：折叠态是鲸鱼娘精灵动画，悬停或单击展开逐行数据面板。

### 形象

- 精灵表 `assets/shenshen-atlas.png`：1536×2288 RGBA、8×11、单格 192×208
- 9 个动作（idle / running / waiting / review / failed / waving / jumping / 左右行走）+ 16 方向注视
- 授权 **CC BY-NC-SA 4.0**，署名与义务见 `assets/ATTRIBUTION.md`（非商业用途）
- 依赖：Python + tkinter + Pillow（系统 Python 3.13 已具备）

### 状态机（与 token 流联动）

| 条件 | 动画 |
|---|---|
| `sessions.status = working` 或最近有采样 | `running`（坐在电脑前） |
| 会话活跃但暂无请求 | `waiting` |
| 一轮刚完成（6 s 内） | `review` |
| 上下文 ≥ 95% | `failed`（同时弹提示 + 提示音） |
| 空闲 | `idle`，鼠标在 520 px 内则按角度注视 |
| 单击 / 双击 | `waving` / `jumping` |

### 交互

| 操作 | 效果 |
|---|---|
| 悬停 | 展开数据面板（移开 0.4 s 自动收起） |
| 单击 | 钉住/取消钉住面板（播放挥手） |
| 双击 | 播放跳跃并打开仪表盘 |
| 拖动 | 移动位置（记住坐标） |
| 右键 | 菜单：仪表盘 / 余额 / 速率口径 / 查询来源 / 大小 / 贴靠 / 漫步 / 注视 / 提示音 / 系统（自启·全屏隐藏·导出 CSV）/ 重置位置 / 退出 |
| `Ctrl+Alt+W` | 全局热键：显示/隐藏面板（可在 `petconfig.json` 改） |
| 托盘 | 左键开面板，右键弹菜单 |

### 面板行含义

`下行` 输出 tokens/s（已扣工具执行）· `上行` 输入 tokens/s · `端到端` 未扣工具口径 + 工具占比 ·
`生成基线` trace 实测解码速度 · `首字延迟` 估算值 · `上下文` 占用 + 进度条 ·
`本会话` / `今日` 累计 · `余额` 当前服务商 · `并行` 同时活跃的会话数

### 配置 `petconfig.json`

```jsonc
{
  "pet":     { "scale": 0.75, "wander": false, "look_tracking": true },
  "alerts":  { "ctx_warn": 0.80, "ctx_crit": 0.95, "credit_warn": 0, "sound": true },
  "system":  { "hotkey": "ctrl+alt+w", "hide_on_fullscreen": true },
  "providers": {
    "active": "",                 // 填 "deepseek" 等服务商 id 才启用余额
    "items": [ { "id": "deepseek", "api_key": "", "base_url": "https://api.deepseek.com" } ]
  }
}
```

余额适配器支持三类：`deepseek`（`/user/balance`）、`openai-compatible`（自定义路径 + 常见字段探测）、
`custom`（任意接口 + `json_path` 取字段，如 `data.0.balance`）。

### 新增文件

| 文件 | 作用 |
|---|---|
| `petsprite.py` | 精灵表加载、帧缓存、BGRA 预乘、动作播放器、16 方向 |
| `petwin.py` | Win32 分层窗口（`UpdateLayeredWindow`）、拖动/点击/穿透 |
| `bubble.py` | 圆角气泡窗口 |
| `providers.py` | 多服务商余额查询 |
| `win32ui.py` | 开机自启 / 全局热键 / 系统托盘 / 全屏检测 |
| `prefs.py` | 配置读写 |

> 仪表盘新增：HERO 里的首字延迟与本轮涨跌、「正在运行」卡片、模型行内速率迷你柱、`导出 CSV` 按钮。

### 网页设置（`/#view=settings`）

总览页右上角「设置」，或直接访问 `http://127.0.0.1:8971/#view=settings`。

| 分组 | 可配置项 |
|---|---|
| 形象与交互 | 宠物大小（50/75/100/125/150%）、随机漫步、注视鼠标、注视角度偏移、注视范围 |
| 速率口径 | 有效（扣工具）/ 端到端 / 生成基线 |
| 告警 | 上下文预警与告警阈值、今日积分阈值、冷却秒数、提示音、气泡 |
| 系统 | 全局热键、全屏自动隐藏 |
| 余额服务商 | 增删改查 + 每个服务商「测试」立即查询（支持 deepseek / openai-compatible / custom） |

保存后写回 `petconfig.json`，桌宠在 **2 秒内热生效**（尺寸、漫步、注视、告警、口径、热键）；
API Key 在页面回显为掩码，留空表示不修改。

> 相关接口：`GET/POST /api/config`、`GET /api/balance?provider=<id>`。

### 两处交互修复（v3.1）

- **随机漫步**：原实现固定 12 s、固定 ±160 px，形成机械往返。现改为随机间隔 9–26 s、
  随机目标（至少偏离一个身位）、随机步速 6–12 px/80 ms，且工作时暂停漫步。
- **注视鼠标**：原逻辑在 1.2 s 的状态泵里更新，且把屏幕角度直接当图集角度用（图集 0° 是正面朝镜头），
  所以又慢又错向。现改为独立 70 ms 循环 + `atlas = (offset − screen) mod 360` 映射 + 方向去抖；
  偏移量可在设置页调（默认 270 = 朝右）。

### 贴靠跟随（v3.2）

开启方式：右键桌宠 → **贴靠窗口并跟随**（右侧灰字显示当前目标），或设置页「贴靠并跟随」。

贴靠目标可在「贴靠目标」子菜单或设置页切换：**WorkBuddy / Qoder**
（按进程镜像名匹配窗口：workbuddy ↔ workbuddy.exe，qoder ↔ Qoder CN.exe / Qoder.exe；
配置项 `dock.target`，未知值会退化为按名字本身匹配，方便以后接别的应用）。

| 行为 | 说明 |
|---|---|
| 跟随位置 | 吸附在 WorkBuddy 窗口右下角，窗口移动/缩放时实时跟随（220 ms 轮询） |
| 非前台隐藏 | WorkBuddy 不在最前面时收起桌宠与面板（默认开启） |
| 全屏隐藏 | WorkBuddy 全屏时隐藏 |
| 最小化隐藏 | 窗口最小化即隐藏，恢复后自动出现 |
| 拖动微调 | 贴靠状态下拖动会记录"距窗口右下角"的边距并持久化 |
| 漫步自动禁用 | 贴靠时不做随机漫步，避免与跟随冲突 |

实现要点：`GetForegroundWindow` + `GetAncestor(GA_ROOT)` 判定前台；`MonitorFromRect` +
`GetMonitorInfoW` 判定全屏；多窗口场景用**句柄粘滞**（锁定已选窗口，失效才重选），
否则两个 WorkBuddy 窗口会让桌宠来回跳。

### 余额并入速率块（v3.2）

余额不再单独占一行、也不再单开气弹出：
面板里 **「今日 ↑… ↓… · ¥0.00」** 同一行显示；右键「查看余额」会临时钉住面板 8 秒并把结果写进这一行，
只有查询失败时才另弹一个小气泡说明原因。

### v3.3 细节修正

**1. 保存入口（原来只有一处，余额卡片下面看不到）**
现在有两处，都调用同一个保存逻辑，提示会同时显示：
- 设置分组底部「保存设置」
- 余额服务商卡片底部「保存余额配置」

> v3.8 起移除了右下角常驻悬浮的第三个「保存设置」按钮：分组重排后两个入口都在视线内，
> 悬浮条只是重复，并且会遮挡余额卡片内容。

**2. 贴靠与随机漫步互斥**
- 界面：贴靠勾选后，「随机漫步」自动取消并置灰，提示「贴靠模式下不可用（桌宠需要跟随窗口）」
- 服务端/配置层：即使直接改 `petconfig.json` 或调 API 打开 `wander`，桌宠检测到贴靠生效后
  会在 2 秒内把它改回 `false` 并写盘（已实测）

**3. "最上层"判定放宽：小弹窗不再触发隐藏**
贴靠模式下判断 WorkBuddy 是否仍在最上层时，以下情况**视为仍然在前台**：

| 情况 | 判据 |
|---|---|
| 本程序自己的窗口 | 前台窗口 PID == 自身 PID（右键菜单 / 气泡 / 面板） |
| 系统菜单、tooltip | 窗口类 `#32768`、`tooltips_class32`、`SysShadow` |
| 系统通知、托盘溢出、任务栏预览 | 对应窗口类名白名单 |
| 从不抢焦点的浮窗 | 带 `WS_EX_NOACTIVATE` |
| 小尺寸弹出窗 | `WS_POPUP` 且宽 < 560、高 < 460 |

只有当用户真正切到一个**正常大小的应用窗口**时，桌宠才会收起。

### 多余额 + cc-switch 风格配置（v3.4）

**面板可同时显示多个余额**（同一行，随面板一起出现）：

```
今日   ↑304.6M ↓1.2M
余额   DS ¥0.00 · OR $0.00
并行   —
```

- 每个服务商有独立「显示」开关，勾上就进这一行；上限由 `providers.max_shown`（默认 5）控制
- 名字自动缩写（DeepSeek→DS、OpenRouter→OR、Moonshot→Kimi、New API→NewAPI）
- 没有可用结果时整行自动隐藏，不占位

**配置模型参照 cc-switch 的 provider**

| 字段 | 说明 |
|---|---|
| `template` | 查询模板：`deepseek` / `moonshot` / `openai` / `openrouter` / `newapi` / `linoroute` / `lmu` / `zhipu` / `custom` |
| `base_url` `path` `json_path` | 路径与取值字段（留空则用模板默认） |
| `scale` | 数值换算（New API 的 quota 单位是 1/500000 美元） |
| `user_id` | New API 站点有时需要的 `New-Api-User` 头 |
| `access_token` | 控制台访问令牌；填了就优先于 `api_key`（linoroute 这类站点的余额必须用它） |
| `extract` | Python 提取器代码，留空用模板内置；契约见 v3.16 |
| `enabled` | 是否在本工具中显示 |

模板内置默认：

| 模板 | 路径 | 取值 |
|---|---|---|
| deepseek | `/user/balance` | `balance_infos.0.total_balance` |
| moonshot | `/v1/users/me/balance` | `data.available_balance` |
| openai | `/v1/dashboard/billing/subscription` | `hard_limit_usd` |
| openrouter | `/api/v1/credits` | `total_credits − total_usage` |
| newapi | `/api/user/self` | `data.quota ÷ 500000` |
| linoroute | `/api/user/self` | 提取器：`data.quota ÷ 500000`，USD |
| lmu | `/v1/usage` | 提取器：`balance` / `remaining`，按 `unit` |
| zhipu | `/api/biz/account/query-customer-account-report` | 提取器：`data.balance`，CNY |
| custom | 自填 | 自填 |

**从 cc-switch 一键导入**

设置页「从 cc-switch 导入」会读 `~/.cc-switch/cc-switch.db` 的 `providers` 表（只读），
列出带 Key 或访问令牌的供应商（显示后 4 位），勾选即导入——**不用重新填密钥**。

- 从 `settings_config.env/auth` 里抽 `*_BASE_URL` 与 `*_AUTH_TOKEN / *_API_KEY`
- 同时读 `meta.usage_script.accessToken` / `userId`（linoroute 这类站点的余额靠它）
- 用 `meta.usage_script.templateType`（`balance` / `newapi` / `custom`）加名称/域名猜模板
- `custom` / `newapi` 会按域名升级成 `linoroute` / `lmu` / `zhipu` 专用模板，并补上对应的提取器
- 官方条目常常没有 base_url，按模板补默认地址
- 同名同地址但**没有 Key** 的预置条目会被"补 Key / 访问令牌"而不是重复新增

实测结果（本机）：DeepSeek、OpenRouter、linoroute、LMU、Zhipu GLM 均已成功返回余额（金额已隐去）。

> 接口：`GET /api/ccswitch`（预览）、`POST /api/ccswitch`（导入）、`GET /api/balance/all`（并发查全部）。

### v3.5 修复：删除按钮没反应

**原因**：删除原来用 `window.confirm()` 二次确认。内置预览面板/嵌入式浏览器会**屏蔽对话框**，
`confirm()` 直接返回 false（有的实现里甚至抛异常），于是点击后什么都不发生——看起来就是"删除不可用"。

**改法**：改成**页内两步确认**，不依赖任何浏览器对话框：

1. 第一次点「删除」→ 按钮就地变成红色「确认删除?」，4 秒内有效
2. 再点一次 → 从列表移除，提示「已移除「XXX」，点保存生效」
3. 4 秒不点则自动恢复原状

服务端已单独验证：新增 → 删除 → 回读，条目数 -1，且**其余服务商的 API Key 不丢**
（因为回传时 Key 是掩码空值，服务端按 id 补齐）。

### v3.6 余额卡与交互约束

**余额卡改为「一行一张」**（面板与网页一致）：整行宽、20px 厂商 logo + 厂名 + 大号金额，
按 `max_shown`（默认 5）全部铺开；查询失败的也保留一行并标红；没有已启用服务商时整块隐藏。
网页端 5 张卡在常见桌面宽度下排成一行，空间不足时自动降到 3+2。

**平台图标**来自本地 `assets/logos/`（站点 favicon / apple-touch-icon + PIL 统一裁切居中，输出透明 64/32/24/20/16 多尺寸；来源与再生成规则见 `assets/logos/SOURCES.md`）：

| 平台 | 图标 | 来源 |
|---|---|---|
| DeepSeek | 蓝色鲸鱼 | deepseek.com |
| Moonshot / Kimi | K 标 / 条纹球 | moonshot.cn / kimi.com |
| OpenAI | 结形标 | openai.com |
| OpenRouter | 方块标 | openrouter.ai |
| linoroute | 蓝色路线标 | linoroute.com/logo.png |
| LMU | 深色 C + 蓝点 | lmuai.ai/favicon.ico |
| New API / 自定义 | 字母徽章 | 本地生成 |

服务端 `/logos/<name>.png` 直出，网页 `<img>` 与桌宠 `PhotoImage` 共用同一套文件。

**交互互斥**（避免自相矛盾的配置）：

| 组合 | 规则 |
|---|---|
| 贴靠 ⇄ 随机漫步 | 贴靠时漫步自动关闭并置灰（桌宠要跟随窗口） |
| 随机漫步 ⇄ 注视鼠标 | 二选一：开漫步自动取消注视，反之亦然 |
| 菜单 | 去掉了重复的「贴靠 WorkBuddy 窗口」，只保留「贴靠并跟随」勾选项 |

**按 cc-switch 清单配置的结果**

| 服务商 | 模板 | 鉴权 | 状态 |
|---|---|---|---|
| DeepSeek | deepseek | Bearer | ✅ 可用（已实测） |
| OpenRouter | openrouter | Bearer | ✅ 可用（已实测） |
| linoroute | linoroute | Bearer accessToken + `New-Api-User` | ✅ 可用（已实测） |
| LMU | lmu | Bearer | ✅ 可用（已实测） |
| Zhipu GLM | zhipu | Bearer | ✅ 可用（已实测） |

> v3.16 更正：这三家此前判为"不可用"是**路径与凭证不对**，不是没有接口——
> linoroute 的余额要用控制台 access token（sk- Key 只用于推理），LMU 在 `/v1/usage`，
> 智谱在 `bigmodel.cn` 的现金余额接口。三家现在都由提取器解析，见 v3.16。

> 新增：`auth` 支持 `bearer` / `x-api-key`（自动附带 `anthropic-version`）/ `both` / `raw`，
> Claude 系代理站点多数需要 `x-api-key` 而非 `Bearer`。

---

### v3.8 多应用化与界面瘦身

**1. 定位从「WorkBuddy 专用」改为「多应用通用」**

数据层早已支持四个来源（WorkBuddy / Qoder / Codex / Claude），但界面文案仍以 WorkBuddy 为主语。本次统一：

| 位置 | 原 | 现 |
|---|---|---|
| 浏览器标题 | `WorkBuddy Token 监测` | `Token 监测 · 多应用` |
| 顶栏副标题 | `WorkBuddy · 本地只读` | `多应用 · 本地只读` |
| 页脚数据来源 | 只列 WorkBuddy 的库表，结尾写"不修改任何 WorkBuddy 数据" | 分应用列出各自读取位置，结尾写"不修改任何应用的数据" |
| 桌宠面板标题（首帧） | `WorkBuddy` | `Token 监测`（有数据后仍显示 `来源·模型`） |
| 托盘提示 | `WorkBuddy Token` | `Token 监测` |

数据源标签、`dock.target` 取值、`workbuddy.db` 等仍保留原样——那是真实的来源标识，不是品牌。

**2. 删除重复显示**

| 删除项 | 原因 |
|---|---|
| HERO 里的「吞吐读数 · 最近 60 分钟」 | 与同区块 live 胶囊、sparkline 自带的「输出/分 · 峰值」重复 |
| 筛选条尾部的「按应用筛选 · 各家可提供的指标不同」 | 选到具体来源时，上方说明条会给出更准确的解释 |
| 右下角悬浮「保存设置」 | 与设置分组、余额卡片的两个保存按钮功能完全重复，且遮挡内容 |

保留的是有信息量的部分：来源计数、来源说明条（解释"为什么是「—」"）、CSV 导出、刷新。

**3. 顺带修掉文档里的重复章节**：`九、桌宠模式` 原来整段出现两次，已删除前一份。

### v3.9 桌面端视觉与网页对齐

原来网页端和 Tk 面板各有一套灰阶与字号（`#6B7280` vs `#6E6E73`、8pt vs 11.5px），
放在一起看像两个产品。这一轮把桌面端接到与网页同一套令牌上。

**1. 共享令牌 `uitheme.py`**

| 类别 | 网页（CSS 变量） | 桌面（Tk 实色） |
|---|---|---|
| 主文本 | `--text #1D1D1F` | `#1D1D1F` |
| 次级 / 三级 | `#6E6E73` / `#7C7C82` | 同值 |
| 外框 / 分隔线 | `rgba(0,0,0,.075)` / `.045` | 叠白底换算成 `#E6E6EA` / `#EFEFF2` |
| 输入 / 输出 | `#0A6ED1` / `#B45309` | 同值 |
| 正常 / 预警 / 告警 | `#0B8A5B` / `#B45309` / `#C4302B` | 同值 |
| 圆角 | 卡片 18 / 小卡 15 | 面板 16 / 余额卡 9 |

上下文占用的配色阶梯也统一成网页那套：**≥50% 琥珀、≥80% 红**。
（与 `petconfig` 里的告警阈值是两件事：颜色是视觉约定，阈值由用户配置。）

**2. 面板重排**

```
● Token 监测                     ● 运行中      ← 标题 + 状态点（只有点带色）
────────────────────────────────────────────
下行 · 输出                        47.6  t/s   ← 主读数 13pt 等宽，单位独立小字
上行 · 输入                       26.1K  t/s
端到端                    63.2 t/s · 工具 24%   ← 次级读数 9pt，弱化
生成基线                          109  t/s
首字延迟                            850ms
上下文                    ▬▬▬▬▬░░░░  62%      ← 圆头进度条
────────────────────────────────────────────
本会话                    ↑3.1M ↓42.0K
今日                   ↑37.5M ↓188.9K
余额                                          ← 分组小标题
[ ▩ DeepSeek                            ¥0.00 ]
[ ▩ OpenRouter                           $0.00 ]
并行                              2 个会话
────────────────────────────────────────────
单击钉住 · 双击仪表盘 · 右键菜单
```

- 余额卡由**直角灰条**改为圆角卡片（`#FAFAFC` + 发丝描边），与网页 `.balitem` 同层次；
  没有可用余额时整块不占位（原来会留一段空白）。
- 数字用等宽字体并单独排单位，刷新时不会左右抖动。
- 标题按**像素宽度**截断（`bbox` 实测），不再按字数硬切，换字体/换缩放都不会溢出。

**3. 跨 DPI 与跨平台**

- 面板几何（宽、内边距、行高、圆角）按 `winfo_fpixels("1i")/96` 等比缩放。
  原来字随系统放大、坐标不放大，125% 下文字会压到行线上；现在 100% / 125% / 150% 都实测过。
- 余额 logo 随缩放选 `@16 / @20 / @24` 三档，避免高缩放下图标糊成一团。
- 字体按本机字体族逐级回退：微软雅黑 → 苹方 → 思源黑体 → Segoe UI；
  等宽：Cascadia → Consolas → SF Mono → Menlo。换系统不会掉到衬线字体。
- 右键菜单套用同一字体与激活底色（Windows 原生菜单只接受部分外观项，已做容错）。

**4. 气泡**

圆角 10→12、内边距 11→12、标题行 16、正文行 20、强调行 28，颜色同面板。
`create_polygon(smooth=True)` 换成与面板一致的"扇形拼接"，小半径下四角不再发鼓。

> 桌面端是常驻进程，**改完要重启桌宠**（右键 → 退出，再双击启动器）。
> 网页端刷新即可。

### v3.10 品牌图标：补齐品牌 + 统一居中

**1. 从 7 个品牌扩到 32 个**

原来只有 deepseek / moonshot / openai / openrouter / newapi 五个真图标，其余全落 `custom`
字母徽章。这次按常用服务商补齐：智谱 GLM、通义千问、Anthropic、Gemini、Mistral、Groq、
Together、硅基流动、Ollama、xAI、零一万物、百川、MiniMax、火山引擎、阶跃、Perplexity、
Fireworks、DeepInfra、ModelScope、Cohere、Novita、GitHub、Vercel、阿里云、腾讯混元。

图标从各站点 favicon / apple-touch-icon 抓取（`favicon.im` 回源；Google 与 DuckDuckGo
的图标服务在本机网络不可达），完整清单与处理规则见 `assets/logos/SOURCES.md`。

**2. 为什么会"看着不居中"**

原图直接按 16×16 贴上去，但各品牌 PNG 的透明留白并不一样：有的四周均匀留 2px，
有的内容只占画布中间 12×8（横向细长的鲸鱼），贴同一位置就会一个偏上、一个偏左；
满幅色块类图标又会显得比别人大一圈。

现在两处一起改：

| 位置 | 改动 |
|---|---|
| 资源 | 统一流水线：裁掉透明边 → 等比缩到 56×56 以内 → 居中贴到 64×64 → 降采样出各尺寸 |
| 面板 | 图标放进 **26px 白色圆角底座**，再按 `img.width()/height()` 的**实际像素**居中（不再按文件名推尺寸） |
| 网页 | 沿用 34px 白底底座，源图换成新处理的版本，视觉尺寸与面板一致 |

**3. 品牌识别抽成一份表**

新增 `assets/logos/brands.json`，网页端与桌宠共用：

- 按 `label` / `id` / `base_url` 做小写子串匹配（例如"Zhipu GLM"→ zhipu、`api.deepseek.com` → deepseek）；
- 猜不到再退回 `template`（deepseek / moonshot / openai / openrouter / newapi）；
- 都猜不到才用 `custom`。

因此 `template: custom` 的服务商也能显示真图标——设置页里"智谱 GLM"现在显示 Z 标而不是通用徽章。
以后加品牌只要放图 + 在 `brands.json` 加一行 `keys`，两端同时生效。

> 服务端新增 `/logos/brands.json` 路由（只放行扁平文件名 + png/json 两种扩展）。
> 服务端还没重启时，网页端会退回内置的精简品牌表，图标仍按 label 正确显示。

### v3.11 修复：桌宠不看向鼠标

**现象**：鼠标在桌宠左上方/右上方移动时，桌宠始终正脸站着；只有某些角度偶尔转一下。

**两个原因**

1. **注视被"忙碌"整段关掉**。原逻辑是 `status == working` 或"最近 120 秒有采样"就
   `return`，而 WorkBuddy 几乎一直有采样——这个功能等于没开。
2. **角度按屏幕真实方位换算**，而图集只有 **16 个水平旋转帧**，没有抬头/低头。
   鼠标在正下方会映射到 180°，看起来是"背过身去"；在正上方映射到 0°，与静止时一样，
   于是"像没反应"。

**改法**

| 项 | 现在 |
|---|---|
| 触发 | 距离 ≤ 320px **一定注视**（生成中也看）；> 400px 才让动画优先（25% 回滞防抖） |
| 角度 | 只取水平分量 `t = dx / √(dx² + (0.55·dy)²)`，见 `look_atlas()` |
| 效果 | 正上/正下都回到正脸，左右最多转到侧脸，**不会出现背面** |
| 远端 | 超过 `look_range`（默认 520）停止注视，恢复 idle 动画 |

> 受素材限制：正上方只能表现为"正视镜头"，没有真正的抬头帧。
> 若以后要抬头/低头，需要补一组俯仰帧。

### v3.12 修复：左右镜像（指右看左）

v3.11 之后用户反馈：**只有上下正常，左右全反**（指左朝右看、右上看右下）。

**根因**：图集方向的语义写反了。`petsprite.atlas_deg()` 的注释按"90° 朝左、270° 朝右"
换算，而实测相反。用像素统计客观判定（转头时脸部肤色像素的横向重心相对剪影中心）：

| 帧索引 | 图集角 | 肤色重心偏移 | 实际朝向 |
|---|---|---|---|
| 0 | 0° | −0.5px | 正脸 |
| 2 / 4 / 6 | 45° / 90° / 135° | +17.5 / +21.4 / +18.2px | **朝屏幕右** |
| 8 | 180° | +1.0px | 背面 |
| 10 / 12 / 14 | 225° / 270° / 315° | −12.4 / −14.6 / −12.4px | **朝屏幕左** |

所以是绕竖轴的**顺时针**转盘：`0° 正脸 → 90° 右 → 180° 背 → 270° 左`。
原公式 `atlas = 270 − 90t` 让"鼠标在右"（t=+1）得到 270°（朝左），左右因此互换；
上下两向都落在 0°/180°，镜像对它无效，所以看不出问题。

**改法**

| 文件 | 改动 |
|---|---|
| `overlay.py` | 新增 `look_atlas(dx, dy, offset)`：`atlas = offset − 270 + 90t`，默认偏移下 `t=+1 → 90°（右）`、`t=−1 → 270°（左）`；`_update_look()` 改为调用它 |
| `petsprite.py` | `atlas_deg()` 修正为 `atlas = screen + offset − 180`，注释按实测朝向重写 |

顺带把角度换算抽成模块级纯函数：上一版验证时是照着公式**重算一遍**再比，等于只验证了
"公式的自洽性"，没有对照素材的真实朝向。现在测试脚本直接调用产品代码里的同一个函数。

> 该精灵表 `lookDirections` 的 `degrees` 字段与"朝屏幕哪侧"是顺时针正向关系，
> 不是镜像；以后若要另做素材，按这个约定标注即可。

### v3.13 右键菜单重做

系统菜单（`tk.Menu`）在 Windows 上由 Tk 按系统画法渲染：蓝色选中条、灰白 3D 边框、
固定行高，字号也只能整体设一个。放在桌宠面板旁边就像两个产品，且菜单长达 17 行。

**改成 Canvas 自绘**（新增 `petmenu.py`，约 380 行）：

| 项 | 系统菜单 | 现在 |
|---|---|---|
| 底与边 | 3D 立体边 | 白底 + 1px 发丝描边 + 12px 圆角（色键抠角，与面板同法） |
| 悬停 | 系统高亮蓝 | `#F1F1F4` 圆角底（与网页 `.seg button:hover` 同色） |
| 行高 / 内边距 | 系统固定 | 30 / 12，全部按 DPI 等比缩放 |
| 勾选 | 系统对勾 | 折线手绘（不依赖符号字体，任何系统都不缺字） |
| 子菜单箭头 | 系统三角 | 2px 折线 chevron |
| 当前值 | 无处显示 | 右侧浅灰小字（如「全部应用」「100%」「WorkBuddy」） |
| 结构 | 17 行平铺 | 13 行 + 分组线：3 种速率口径收进「速率口径」子菜单 |

**菜单变成"纯渲染"**：每次右键都按当前变量/配置重建一份 spec，不再维护
`Checkbutton` 的勾选态与 `entryconfig` 的标签同步。因此热重载后菜单永远和配置一致——
原来「贴靠目标」换了、菜单标签还停在旧值的问题也就消失了。

**文案中性化**：`贴靠 WorkBuddy 并跟随` → **`贴靠窗口并跟随`**，当前目标以灰字显示在右侧，
换目标只需看子菜单，标题不再绑死某个应用。

**交互细节**

- 子菜单跟随鼠标悬停展开，移回父级自动收起（170ms 去抖）；
- 子面板不抢焦点（抢了会让父面板收到 `FocusOut` 把自己收掉）；
- 点到其它窗口时靠 `FocusOut` 收起，带 260ms 宽限，避免焦点抖动误收；
- 菜单自动避让屏幕边缘，右侧放不下时子菜单翻到左边。

### v3.14 修：今日积分提示反复弹

**现象**：桌宠每隔 5 分钟弹一次「今日积分已达 1261（阈值 500）」，一整天不停。

**原因**：`alerts.credit_warn` 被设成 500，而当天实际积分约 1267（WorkBuddy 730 + Qoder 537）。
原逻辑只要"超过阈值"就弹，冷却 `cooldown_sec`（默认 300 s）过后再弹一次——阈值一旦被越过，
提示就变成每 5 分钟一次的复读。

**改法**

| 项 | 原来 | 现在 |
|---|---|---|
| 触发 | 超阈值后每 cooldown 弹一次 | 按**档**：跨过 500 / 1000 / 1500 各弹一次（`credit_band()` 纯函数） |
| 跨天 | 无处理 | 积分回落（或调高阈值）自动回到 0 档，第二天重新计 |
| 标题 | 统一「提示」 | 「积分提醒」 |
| 数值 | `1261` | `1,262`（千分位，一眼能读） |

**顺带修掉气泡截字**：`Bubble` 的宽度原来固定 210px，只够短句；"今日积分 1,262 · 阈值 500"
会被裁掉右半截。现在按 `tkfont.measure()` 实测最长一行自适应（上限 460px），
短提示仍是 210px，不会突然变宽。

**不想要这个提醒**：设置页「今日积分阈值」设为 `0` 即关闭；嫌 500 太低可以改成 1500 / 2000。

### v3.15 修：菜单里的勾选项点了没反应（v3.13 的回归）

**现象**：右键点「贴靠窗口并跟随」，勾不上去，桌宠也不跟窗口；随机漫步、注视鼠标、
提示音、开机自启、全屏隐藏同样无效。

**原因**：`tk.Menu` 的 `Checkbutton` 在回调**之前**会把关联变量翻转，所以
`_toggle_dock` 这些处理器的写法都是"读变量 = 新状态"：

```python
dock["enabled"] = bool(self.var_dock.get())   # 期望这里已经是新值
```

v3.13 换成自绘菜单后，没有任何控件去翻转变量，于是每次读到的都是旧值——
表现就是"点了没反应"，但菜单渲染、收起、单选子菜单都正常，所以没被察觉。

**改法**：`_menu_spec` 里六个勾选项统一走 `_flip(var, handler)`，补回这层语义：

```python
def run():
    var.set(not bool(var.get()))   # 先翻转
    fn()                           # 再交给原处理器
```

单选类（速率口径 / 查询来源 / 宠物大小 / 贴靠目标）不受影响——它们本来就显式
`var.set(value)` 再回调，已逐项验证：状态、勾选态、右侧提示三者同步。

**验证**：桩对象逐项点击——六个勾选项的变量都正确翻转、处理器读到新值、重新生成菜单
时勾选态同步；四个单选子菜单的设置值与提示文字一致。

### v3.17 多应用自动跟随 + 一手 token 数据

**问题**：仪表盘虽然有 WorkBuddy / Qoder / Codex / Claude 四个来源，但此前自动模式主要
看"最近有没有请求"，切到对应应用后经常要等十几秒才有反应；Codex / Claude 的历史数据
还要靠 cc-switch 间接补；Qoder 没有 token 时，面板和基线表会挂着 0 t/s 或空白上下文。

**做法**：

- **自动跟随**：综合最近活动时间、当前前台进程和 20 秒防抖；用户主动切到某个应用时
  立即跟随，后台应用偶发请求不会把标签来回抢。新增 `source=auto` 白名单与
  `/api/live` 的 `requested_source` / `units` 字段。
- **一手数据优先**：Codex 直接读原生 `sessions/**/rollout-*.jsonl`（token、真实首字延迟），
  Claude 直接读 `projects/**/*.jsonl`；cc-switch 只在原生数据缺失时补历史与 TTFT，
  不再反向覆盖一手记录。Qoder 明确只有 credits 与 `context_usage_ratio`，不伪造 token。
- **单位自适应**：面板和看板按来源显示 `tokens/s`、`credits/min`、`requests/min`；
  标题、排序、燃烧率与基线卡随单位切换，Qoder 不再出现一排 0 t/s。
- **模型后面标 agent**：生成基线表在模型名后标注实际调用它的来源
  （WorkBuddy / Qoder / Codex / Claude），同一模型被多个 agent 使用会并列显示；
  悬停可看各自采样次数和其中子 agent 的请求数。
- **面板细节**：速率单位按字体实际宽度让位（修复 `req/min` 压住数字）；服务未启动时
  本地兜底保留上下文与活跃状态，并会在连续取不到数据时自动拉起 `server.py`。
- 重写随机漫步与鼠标注视的更新路径，减少状态泵抖动；对应开关在设置中按需开启。

**验证**（2026-10-23，本机）：自动源在前台切换到 Codex 时立即生效，后台请求防抖正常；
基线表 905 次 `DeepSeek-V4.1-Flash` 采样正确并列 WorkBuddy / Qoder，Codex 行并列 Claude；
停掉服务后桌宠在约 9 秒内自动恢复；看板无控制台报错。

### v3.16 余额提取器：linoroute / LMU / Zhipu GLM

**问题**：这三家在设置页一直是"未启用 + 备注原因"，但站点其实都有余额接口——
之前用通用的 `newapi` / `custom` 模板去套，路径和凭证都不对：

| 服务商 | 之前 | 实际 |
|---|---|---|
| linoroute | 拿 sk- Key 查 `/api/user/self` → "access token 无效" | 要用**控制台的 access token**，并带 `New-Api-User` |
| LMU | 试了标准路径全 404 | `GET /v1/usage` 直接返回 `balance` / `remaining` / `unit` |
| Zhipu GLM | 判为"官方无余额接口" | `GET bigmodel.cn/api/biz/account/query-customer-account-report` 返回 `data.balance` |

**做法**：把 cc-switch 的 `usage_script` 模型搬过来——每个服务商可以带一段
**Python 提取器代码**，请求仍然是 `base_url + path + 鉴权头`，拿到 JSON 后交给
`extract(response)` 自行取值。字段名与 cc-switch 的 extractor 对齐，翻译时不用改结构：

| cc-switch (JS) | 这里 (Python) |
|---|---|
| `remaining` | `amount` / `remaining` |
| `unit` | `currency` / `unit` |
| `extra` / `planName` | `detail` / `extra` / `plan` |
| `isValid: false` + `invalidMessage` | `ok: false` + `error` / `invalidMessage` |

```python
def extract(response):
    d = response["data"]
    return {"amount": d["quota"] / 500000, "currency": "$",
            "plan": d.get("group")}
```

- 提取器在**受限命名空间**里执行，只暴露 `abs/float/int/len/round/...` 等安全内置，没有 `import` / `open`
- `access_token` 优先于 `api_key`；UI 里留空表示不修改，服务端按 id 补齐，不会被掩码回写清掉
- 设置页每个服务商卡片下有可折叠的「提取器代码」编辑区；留空则用模板内置代码
- 导入 cc-switch 时会带出 `accessToken` / `userId`，并把 `custom` / `newapi` 升级成专用模板

**实测**（2026-10-21，本机 Key）：linoroute、LMU、Zhipu GLM 三个提取器均返回正确余额（金额已隐去），
总览页 5 张余额卡全部可用，`/api/balance/all` 返回 5/5。

**图标**：按 `assets/logos/SOURCES.md` 的流水线联网取图——linoroute 用站点 og:image 的
1024px 品牌标记，LMU 用 favicon.ico 的 64px 帧，统一裁切居中后输出 64/32/24/20/16 五档；
`brands.json` 增加 `linoroute` / `lmu` 两条识别规则，网页与桌宠共用。

> 注意：修改的是 Python 代码，需重启服务（`stop.cmd` → `start.cmd`）才会生效；
> 余额查询本身是 GET，不改动任何服务商的数据。

## 十、怎么启动（v3.7）

**最简：双击桌面上的「启动 WorkBuddy 桌宠.cmd」**

它做三件事，且都是幂等的：起本地服务（无窗口）→ 起桌宠 → 打开仪表盘。
桌宠已在运行时再双击，只会再打开一次仪表盘，不会重复启动。

已放好的两个入口：

| 位置 | 文件 |
|---|---|
| 桌面 | `<你的桌面>\启动 WorkBuddy 桌宠.cmd` |
| 开始菜单 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\启动 WorkBuddy 桌宠.cmd` |

**想要带图标的正式快捷方式（3 步，系统自带功能最稳）**

1. 右键 `启动 WorkBuddy 桌宠.cmd` → **发送到 → 桌面快捷方式**
2. 右键新快捷方式 → **属性 → 更改图标**
3. 选 `<项目目录>\assets\pet.ico`

> 图标已生成：精灵 idle 首帧，256/128/64/48/32/16 多尺寸（114 KB）。
>
> 为什么没直接给 .lnk：本环境禁止脚本实例化系统托盘/快捷方式所需的 COM 组件；
> 改用纯二进制格式手写 Shell Link 后，系统判定为无效
> （`WinError 1155 没有应用程序与此操作的指定文件有关联`）。所以走 .cmd + 系统"发送到"。

**启动器内部**：`app.pyw`（由 pythonw 运行，全程无黑窗口）→ 探 8971（服务）/ 8799（桌宠单实例锁）
→ 缺哪个补哪个 → 打开仪表盘。

**其它启动方式**

| 方式 | 命令 |
|---|---|
| 一键脚本（会闪一下控制台） | 双击 `start.cmd` |
| 只开服务 | `pythonw server.py --no-browser` |
| 只开桌宠 | `pythonw overlay.py` |
| 停止 | 双击 `stop.cmd`，或右键桌宠 → 退出 |
