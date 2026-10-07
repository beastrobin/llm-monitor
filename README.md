# LLM 监控工作台

**当前版本 v1.2** —— 查版本：`python monitor.py --version`，或看面板标题旁的小徽章
（它读的是服务端下发的值，不是页面里写死的）。改动历史见[版本记录](#版本记录)。

本地大模型推理 + 硬件的实时监控面板。**零第三方依赖**——只用 Python 标准库，
GPU 走 `nvidia-smi`，系统指标走 ctypes 调 Win32 API。

## 快速开始

双击桌面上的 `monitor` 快捷方式（等价于）：

```bat
wscript.exe start_monitor.vbs
```

浏览器打开 <http://127.0.0.1:8090>。

想用**功耗控制**（面板上唯一会改机器设置的控件），改用：

```bat
wscript.exe start_monitor_admin.vbs
```

它只多做一件事：弹一次 UAC 提权，然后转调 `start_monitor.vbs`。原因见
[显卡功耗控制](#显卡功耗控制)。纯看数据不需要提权。

**为什么是 VBS 而不是 bat。** 这两个脚本从 `wscript.exe`（GUI 子系统）启动，服务用
`pythonw.exe` 跑 —— 整条链上没有任何控制台。早先用 `.bat` 配 `WScript.Shell.Run(cmd, 0)`
隐藏窗口，那个窗口样式只有老的 `conhost` 认；Windows 11 默认把新控制台交给
**Windows Terminal** 托管，样式被忽略，于是双击后会留下一个关不掉的黑窗口。换成
`wscript.exe` + `pythonw.exe` 之后，没有控制台可以被接管。
副作用是输出没有地方可看：`pythonw.exe` 下 `stdout` 是空的，所以 `log()` 同时写一份
`run.log`，排查启动失败先看它。

命令行参数可覆盖配置（原样透传给 `monitor.py`）：

```bat
wscript.exe start_monitor.vbs --port 8090 --interval 2
```

**重复启动会被拒绝，并且会告诉你为什么。** 服务用 `SO_EXCLUSIVEADDRUSE` 独占端口，
第二次启动不会和已在跑的那份抢 8090。Windows 上 `http.server` 默认的 `SO_REUSEADDR`
是允许两个进程绑同一个端口的，两边都会 accept，页面就会随机读到另一个进程的数据 ——
所以这里显式关掉了。

被挡下时 `monitor.py` 会先问一句占着端口的那份是谁（读它的 `/api/snapshot`），再按结果
返回不同的退出码。启动器不等待服务（它要活到被 kill 为止），所以这些码只会写进 `run.log`，
含义是：

| 退出码 | 谁占着端口 | 含义 |
|---|---|---|
| 1 | — | 真实错误（如 `config.json` 读不了），原因已写进 `run.log` |
| 2 | **提权**的面板 | 那份已经能改功耗，不需要第二份 |
| 3 | **只读**的面板 | 那份只能看；接管要先 `taskkill`，命令写在 `run.log` 里 |
| 4 | 不是面板的其他进程 | 端口上是别的东西，浏览器不会被打开 |

启动器自己也不靠退出码决策：它先用一次 HTTP 探测（走 `WinHttp`，不经过系统代理）判断面板
是否已经在跑，在跑就直接开浏览器。所以双击快捷方式而面板其实已经在跑时，你看到的是浏览器里
那个面板，而不是一句报错。只想确认状态、不想开浏览器的话，直接跑 `python monitor.py` 读退出码。

## 界面

- **曲线可悬停取值**：鼠标移到任意迷你图上会出十字线，并显示该处的数值。
- **每张卡片右侧的 ⓘ** 是该项口径说明（分母是什么、阈值多少、什么时候会变红）。
- 卡片脚注写的是**带标签**的数字，例如"上限 262,144 tok · 空闲残留"，不出现孤零零的比值。
- `warn` / `fail` 级别的结论（温度偏高、接近上限、采集失败）直接写在卡片脚注或端点上，
  不藏进 ⓘ。
- 深浅色跟随系统（`prefers-color-scheme`），也可以手动切换，选择记在 localStorage。
- 页脚会显示曲线历史点数与覆盖时长。
- **顶栏那个小圆点表示数据通道，不是被监控的端点**（端点状态在各自的卡片上）。三态：
  绿 = 实时推送中；黄 = 推送不可用、已降级为**定时轮询**；红 = 断开、正在重连。
  鼠标悬停有文字说明。

### 自动刷新是怎么保证的

两条独立通道，谁能用谁上：

1. **SSE**（`GET /api/stream`）—— 正常情况。每个采样周期推一帧，立刻渲染。
2. **看门狗轮询** —— 如果连续 8 秒没收到任何帧，自动改用 `GET /api/snapshot` 每 4 秒拉一次。
   一些内嵌浏览器和预览面板会直接屏蔽 `EventSource`，这种情况下只靠 SSE 就等于**页面永远
   停在打开那一刻** —— 而"你自己刷新一下"不是个答案。

推送恢复后轮询会自己停（两者用独立时间戳，流一旦有帧就不再轮询），不需要手动切回。

**每帧都带完整快照**，不只是扁平化的标量序列。卡片读的是快照，所以只带序列的帧会让
**曲线继续走、所有数字冻住** —— 这个 bug 真出现过一次，`tools/render_check.js` 现在会
同时检查帧契约和"推一帧后数字是否真的变了"。

## 监控什么

**推理**（来源：llama-server 的 `/metrics`、`/slots`、`/props`）

| 指标 | 口径 |
|---|---|
| 生成速度 | Δ`tokens_predicted_total` ÷ Δ`tokens_predicted_seconds_total`（瞬时），另附 counter 累计均值 |
| Prefill 速度 | Δ`prompt_tokens_total` ÷ Δ`prompt_seconds_total` |
| MTP 命中率 | `spec_decode_num_accepted_tokens_total` ÷ `spec_decode_num_draft_tokens_total` |
| MTP 每步接受 | accepted ÷ `spec_decode_num_drafts_total` |
| MTP 逐位置命中 | `..._accepted_tokens_per_pos_total{position=N}` ÷ 验证步数 |
| MTP 是否启用 | preset 启动参数里的 `--spec-type` —— **不是** `/props` 的 `speculative.types` |
| KV 缓存命中 | `prompt_tokens_cached_total` ÷ (cached + 新建) |
| 上下文水位 | `/slots` 的 `n_prompt_tokens + n_decoded` ÷ `/props` 的 `n_ctx` |

**`speculative.types` 不可信**：MTP 用 `--spec-type draft-mtp` 跑起来时，`/props` 里这个字段
照样报 `none`，slot 里也可能不带这个键。所以卡片上的 MTP 标签取自 `/v1/models` 回传的 preset
启动参数。本机实测 6 个 preset 里只有 `Qwen3.6-35B-A3B-UD-Q4_K_M` 与 `Qwen3.8-27B-UD-Q4_K_M`
真的开了 MTP，其余 4 个没有 —— 光看 `/props` 会把它们全判成同一档。

**上下文水位的"空闲残留"**：请求结束后服务端会清掉 slot 的提示词计数（`n_prompt_tokens`
变成 `null`），此时分子只剩 `next_token[].n_decoded`。卡片会标出"空闲残留"，避免把这个
残值当成实时水位。

**显卡**：利用率、显存占用与带宽、温度、功耗（含上限）、风扇、SM/显存频率、PCIe 链路、计算进程归属。

**系统**：CPU 总负载、每核频率、物理内存、提交内存（commit charge）、系统缓存、句柄/进程/线程数、固定分区容量、`llama-server` 进程的 host 内存占用。

## 显卡功耗控制

显卡区下方的档位条对应 `nvidia-smi -pl`，也就是显卡的功率墙。满载时 GPU 会在这个上限
附近降频，功耗不会超过去。

**档位跟着显卡走，不是写死的。** 菜单在运行时按显卡自己上报的 `power.min_limit` /
`power.max_limit` 推导：

- 窗口能覆盖 600 W 级的卡（最大上限 ≥ 575 W）用实测标定过的那一套
  **150 / 300 / 400 / 600 W**（静音 / 低耗 / 均衡 / 复原）。400 W 是 150–600 W 功耗曲线的
  能效顶点：曲线在 450 W 之后变平，那里是 ~95% 峰值速度下最好的 t/s-per-watt。见 `power_curve/`。
- 其它任何卡按自己最大上限的 **40 / 60 / 80 / 100 %** 生成四档，取整到 5 W。比如
  RTX 5090 Laptop（5–175 W）得到 70 / 105 / 140 / 175 W；一台 300 W 的桌面卡得到
  120 / 180 / 240 / 300 W。

菜单只在一个地方生成（`collectors/power.py` 的 `presets_for()`），写接口做白名单校验时用的
就是下发给面板的同一份列表，所以"面板上有的按钮"和"接口肯接受的值"不可能各走各的。前端的中文
档位名按**角色**映射（静音/低耗/均衡/满血），与瓦数解耦；600 W 级卡上标定出来的实测数字，只在
菜单确实来自那份标定表时才附加显示。

**笔记本 GPU 接受不了这个设置，而且拒绝方式很阴。** 这不是权限问题：驱动对 `nvidia-smi -pl`
直接回 `Changing power management limit is not supported in current scope`，**退出码却仍然是
0**，还照样打印 "All done."。所以本工作台不看退出码，而是解析驱动自己的输出；识破后回
`unsupported`，面板随即撤掉档位按钮并改报实测上限。笔记本的上限由 Dynamic Boost 在出厂 TGP
区间内自主调整（5090 Laptop 实测在 110–175 W 之间浮动），要改只能走厂商控制中心或 NVIDIA
控制面板里的性能模式，`nvidia-smi` 这条路是关着的。

**两条功耗读数不是一回事。** `power.limit` 是**软件功耗墙**，即 `-pl` 写进去的那个值；笔记本
GPU 根本不报这一列（永远 `[N/A]`）。`enforced.power.limit` 是**驱动此刻真正在生效的上限**，
任何卡都报，且在笔记本上会随 CPU 负载与温度浮动。面板优先用软件墙做分母；没有软件墙时改用生效
上限，并在读数后面标「动态」，同时把 `出厂默认` 一并显示出来当参照。功耗卡和档位条用的是同一
套回退规则，所以两处的"上限"永远指同一个数。

**需要管理员权限。** 这不是本工作台加的限制 —— Windows 上非管理员执行 `nvidia-smi -pl`
会被直接拒绝，退出码 5（`ERROR_ACCESS_DENIED`），改动根本不会发生。本机实测过：用受限令牌
（`runas /trustlevel:0x20000`）运行时，连 `--query-gpu` 都返回"拒绝访问"。所以
`monitor.py` 启动时会做一次权限自检并把结论写进 `run.log`；面板上无权限时档位条是禁用的，
并且直接写明该改用哪个脚本启动。

**重启电脑后恢复默认。** nvidia-smi 的设置不持久。要开机自动应用，用任务计划程序
（勾选"使用最高权限运行"）跑一个 `nvidia-smi -pl 450`。

**能省多少、会慢多少。** 出字（decode）阶段卡在显存带宽，限功耗主要砍的是 SM 算力，
所以适度下调通常几乎不掉速；预填充（prefill）阶段卡在算力，会实打实变慢，但它只占一次
请求开头的一小段。有个可对照的社区实测：RTX 5060 Ti 从 180 W 降到 150 W 底线，吞吐只从
81.2 掉到 80.4 tok/s。**但这个比例不能直接套用** —— 那是把 180 W 卡降到 83%，而 150 W
只有本机型 600 W 的 25%，降这么狠会开始碰到真实瓶颈。

**只接受预设档位。** 接口用白名单校验（就是下发给面板的那份菜单），且写入前会读一次显卡报告
的 `power.min_limit` / `power.max_limit`，超出范围直接拒绝，而不是交给驱动去静默钳制。

**写完必须回读，且回读方式分两种。** 驱动可以拒绝或取整，档位高亮也以实测值为准 —— 万一别处
改了功耗，按钮跟着现实走，而不是跟着"我上次请求了什么"。驱动肯报软件墙时直接比对；不肯报时
（笔记本）退一步拿生效上限做旁证 —— 功耗管理算法永远不会把上限抬到软件墙之上，所以"上限明显
高于刚请求的值"就说明这次写入没落地，那种情况回 `not_applied`，而不是报成功。两条路都读不到
时回 `verified: false`，面板把话术改成"已下达 X W（驱动不回读）"，不假装确认过。

### 这个写接口是怎么防跨站的

`POST /api/power-limit` 是这个服务上唯一的写操作，而浏览器里任何页面都能向
`127.0.0.1:8090` 发请求。三层校验：

1. **Host 必须是回环地址** —— 挡 DNS rebinding（攻击者域名解析到 127.0.0.1，浏览器就会
   把它当成同源）。
2. **Origin 存在时必须是回环** —— 挡跨站表单提交。
3. **必须带自定义头 `X-LLM-Monitor: 1`** —— 这层才真正起作用：自定义头会强制 CORS 预检，
   而服务端从不响应 `OPTIONS`，跨站脚本连请求都发不出来。普通表单能过前两层，但设不了
   自定义头。

三条路径实测分别返回 `403 bad_host` / `403 bad_origin` / `403 missing_header`。

## Token 账本

推理区下方是一份跨重启的 token 用量账本，回答三个问题：**今天用了多少**、**一共用了多少**、
以及**这些量是在显卡哪个功率档下跑出来的**。

**为什么非要一个账本。** llama-server 只把 token 数报成**进程生命周期内的累计值**
（`prompt_tokens_total` / `tokens_predicted_total`），server 一重启、或者 router 卸载了模型，
这些数就回到 0。面板自己也不留历史 —— 曲线缓冲只有 900 点（默认约 30 分钟）且全在内存里。
所以直接读 counter 只能回答"这个 server 进程起来之后用了多少"。

**怎么算的。** 每轮采样读一次原始 counter，与上次的值相减得到增量，累加进 `stats.json`。
关键在于**文件里同时存着上一次读到的原始计数值**：面板重启后拿它当基线继续减，于是既不会
丢掉停机期间产生的量，也不会把 server 的历史总量重复计一遍。counter 变小说明 server 重启过
（或模型被卸载），此时从 0 重新起算，并记一次"归零"。

| 数字 | 口径 |
|---|---|
| 今日生成 | `tokens_predicted_total` 的当日增量 |
| 今日预填充 | `prompt_tokens_total` 的当日增量（**只含实际参与计算的**） |
| 累计生成 / 累计预填充 | 同上，跨日累加 |
| KV 复用（副行） | `prompt_tokens_cached_total` 的增量，即走缓存没有重算的那部分 |

预填充与 KV 复用是**互斥的两部分**：前者真的跑了前向，后者直接从 KV 缓存拿。所以"输入总量"
是两者之和，卡片上的"今日预填充"只统计前者 —— 本机实测过 cached 比 prompt 还大的情况。

### 三个下钻维度

同一份增量会同时记进三个不同切分方式的桶，所以每笔量都能被追问下去：

| 区块 | 切分 | 回答的问题 |
|---|---|---|
| 近 7 天汇总 | 日期 | 哪天跑了多少 |
| 按功率档归集 | 显卡功耗上限 | 这些量是在哪一档功耗墙下跑出来的 |
| 明细 | 日期 × 功率档 × 模型 | 某天、某档位、某个模型各跑了多少、多快 |

**速度**用的是引擎自己上报的耗时（`prompt_seconds_total` / `tokens_predicted_seconds_total`），
不是墙钟。这两个计数器只在引擎真正计算时增长，所以空闲时间不会把速率摊薄 —— 它反映的是
"干活时有多快"，和面板上那两张速度卡是同一套口径。**预填充速率的分子含 KV 缓存命中的 token**，
而缓存重读几乎不费时间，所以命中率越高这个数越大；对着明细表里的"KV 复用"一列看就能判断出来。
某个组合的耗时计数为 0 时显示 `—`，不假装是 0 t/s。

**功率档是按时间归因，不是按用量分摊。** 功耗上限属于整张显卡，同一张卡上同时跑的几个模型
都会被记进同一个档位，所以这个数永远不代表"某个模型耗了多少瓦"。它只说明"产生这些 token 的
那一刻，卡被限在多少瓦"。档位归集从**本功能启用那一刻**开始记，之前的 token 没有记录当时的
档位，所以各档之和小于累计总量 —— 页面上有一条说明直接写着起始时刻。采样时读不到功耗上限
（显卡采集关闭或驱动查询失败）会单独记成"档位未知"并显示为警告，不会被并进任何一档。

**档位记的是"当时生效的那个值"。** 优先软件功耗墙（就是你点的那一档）；笔记本这类不报软件墙的
卡退回到驱动的生效上限，并**取整到 5 W** —— 那个上限每秒都在动，逐瓦记一档只会把账本打碎成
一堆没有意义的行。取整得来的档位在面板上显示成 `≈170 W`，一眼能看出它不是从菜单里选的；
`175 W 满血` 这种才是。档位行数有上限（`TIER_CAP`），超出后按量级保留前面几档。

**落盘。** `stats.json` 与面板同目录，写入先落 `.tmp` 再改名，中途崩溃不会留下半个文件。
数据有变化时每 15 秒写一次，Ctrl-C 退出时补一次。文件损坏不影响启动，只是降级成"只在内存里
记账"，页面上会直接显示一条黄色提示（这类结论不进 hover）。

**边界**：面板停机期间如果 server 也重启过，那段时间的量会归到重启后的计数上，无法精确切分。
另外首次见到某个模型时，会把它当时已有的累计值整体计入 —— 否则那部分就永久丢了。
但**"这个模型没见过的某个字段"不走这条规则**：那种情况只把当前值当基线、不计入任何量。这条
区别很关键 —— 加 `*_seconds` 那次忘了区分，结果给已在跟的模型记进了几分钟的耗时却对应 0 个
token，所有速率都算成了"没有数据"。旧版 `stats.json`（v1，没有档位与明细）能直接读，只是
新的两个维度从升级那一刻开始重新累计。

## 配置

`config.json` **不在仓库里** —— `.gitignore` 把 `*.json` 一并排掉了，因为它按机器而定。直接跑
`monitor.py` 会顺手生成一份默认配置，但默认配置里 `api_key` 是空的，而 hermes 每次启动都会换
一个新 key，于是面板会以 401 开场、一个模型都不显示（报错只说 `unauthorized`，看不出是配置问题）。
正常启动路径不用管这件事：`start_monitor.vbs` 会先跑一次 `tools/sync_hermes_key.py`，把 router
当前的 key 与日志路径抄进 `config.json`。手动修也行：

```bash
python tools/sync_hermes_key.py          # 同步 port / api-key / live.log
python tools/sync_hermes_key.py --check  # 只报告差异，不动文件
```

它幂等且克制：key 没变就不写文件，找不到 router 日志就不碰配置（面板照旧报 401，不会更糟）。

编辑 `config.json`：

```json
{
  "server": { "host": "127.0.0.1", "port": 8090, "interval_seconds": 2.0 },
  "endpoints": [
    {
      "id": "hermes",
      "label": "Hermes Router",
      "base_url": "http://127.0.0.1:18434",
      "api_key": "...",
      "api_key_env": "HERMES_LLAMA_API_KEY"
    }
  ],
  "stats": { "enabled": true, "path": "stats.json", "keep_days": 90 },
  "live": { "enabled": true, "log": "D:/AI/hermes/logs/llama-server.log" }
}
```

- `api_key_env` 里的环境变量**优先于** `api_key` 字段。不想把密钥留在文件里就设它。
- `live.log` 是 router 的启动日志。`live` 端点**优先直接问 router 的 `/v1/models`** 判断当前
  模型，日志只是兜底；但日志路径还兼一个用途 —— 从它反推出 hermes 的 `config.yaml` 在哪，
  端点地址就从那份配置里读。**一般不用手填**：启动时 `tools/sync_hermes_key.py` 会探测并写回。
  候选是 `%LOCALAPPDATA%\hermes\logs\llama-server.log` 与 `D:\AI\hermes\logs\llama-server.log`，
  取存在的里面最新的一个；显式配了 `live.log` 且文件在，就以它为准。真配错了也不会只剩一句报错
  —— 卡片上会直接写出它找过的位置。
- 端点可以配多个，router 模式和单模型模式会自动识别。
- `kind` 声明这个端点说的是哪种方言。**不写就是 `llama`**（Prometheus 文本 `/metrics`），
  `"kind": "strata"` 表示 Strata 服务（JSON `/metrics`）。方言是服务端的属性，所以配在配置里
  而不是靠探测 —— 探测要在每次启动时都跑一遍（包括那个服务没起的启动），而且对"长得像
  llama.cpp 但不是"的服务照样会猜错。写错的 `kind` 会在启动日志里点出来并退回 `llama`。

```json
{ "id": "strata", "label": "Strata", "kind": "strata", "base_url": "http://127.0.0.1:8200" }
```

- 服务未启动的端点不会拖慢采样，只是显示为离线。
- `stats.enabled` 设成 `false` 就关掉 token 账本（既不读也不写）；`stats.path` 的相对路径按项目
  根目录解析，父目录不存在会自动创建；`stats.keep_days` 控制每日明细的保留天数，超期的"日桶"
  会被丢掉，但累计总量不受影响。

### 怎么拿到 hermes 当前的端口和密钥

两件事分开看：

**端口（端点地址）不用管。** hermes 每次启动都会换一个端口，并把结果写回自己的
`config.yaml`（`model.base_url`）。面板以那一刻的 `config.yaml` 为准，并且**每 15 秒重读一次**，
所以 hermes 重启后地址变了会自动跟上；上一次采样发现 router 没应答时更会**立刻重读**，不等
这 15 秒。`config.json` 里的 `base_url` 只是兜底（机器上没有 `config.yaml` 时用）。哪个文件被
采纳写在启动日志的 `hermes router:` 那行；地址真的变了会另起一行 `hermes router moved:`。

**密钥仍以启动日志为准。** api-key 不写在 `config.yaml` 里，只在日志的 `# spawn: [...]` 行：

```bash
grep -- "--api-key" "D:/AI/hermes/logs/llama-server.log" | tail -1
```

这行同时给出 `--port` 和 `--api-key`。同步这件事已由 `tools/sync_hermes_key.py` 自动完成，
上面这行只在排查时用得上。注意**端口跟着 `config.yaml`、密钥跟着日志**：拿它手工对配置时别把
两者混在一起用。

### 让 llamacpp-loader 也能被监控

loader 起的 server 默认**不带** `/metrics` 端点，需要在 GUI 的 **Advanced** 页把
**Metrics** 打开（对应启动参数 `--metrics`）。`/slots` 是 llama.cpp 默认开启的，无需设置。

## 实现上的几个坑

1. **router 模式下 `/metrics`、`/slots`、`/props` 必须带 `?model=<id>&autoload=false`**，
   否则一律返回 400。本工作台会先从 `/v1/models` 挑出 `status.value == "loaded"` 的模型，
   再逐个带上参数去拉。
2. **`llamacpp:predicted_tokens_seconds` 这个 gauge 在空闲时读 0**，直接显示会把"空闲"
   误报成"卡死"。所以速率一律由 counter 差分算出。
3. **`urllib` 在 Windows 会读取 `http_proxy` 环境变量**，导致请求 `127.0.0.1` 被送到代理上
   而失败。采集器显式使用了空的 `ProxyHandler`。
4. **`nvidia-smi -pl` 有三种失败方式，退出码只认得出第一种**：非管理员下退出码 5、输出"拒绝
   访问"；笔记本上输出 `Changing power management limit is not supported in current scope`
   而**退出码是 0**，只看返回码就会以为改成功了；还有第三种 —— 命令被接受、值根本没变。所以
   写完一律回读：驱动肯报 `power.limit` 就直接比对，不肯报（笔记本）就拿 `enforced.power.limit`
   当旁证（功耗管理算法不会把上限抬到软件墙之上），两条都读不到就明说"验证不了"。档位高亮同样
   以实测值为准 —— 万一别处改了功耗，按钮跟着现实走，而不是跟着"我上次请求了什么"。
5. **推送的帧内容是一份契约，两边必须同时改**。`hub.broadcast()` 原来只发
   `{point: {ts, values}}`，而卡片渲染读的是 `S.snapshot` —— 于是曲线照常延伸、**所有数字
   停在建连那一刻**，看起来就像"页面不自动刷新"。首帧是好的（它带 snapshot），所以只打开
   页面看一眼完全看不出问题。改法：每帧都带 `{snapshot, point}`。
6. **jsdom 里加了 `setInterval` 之后，node 不会自己退出**。烟测新加的看门狗让事件循环永远
   不空，脚本一直挂到被 SIGTERM 杀掉，于是**真实退出码被 SIGTERM 顶替**，看起来像失败了。
   所以报告写完要显式 `process.exit(code)`（用回调形式，先冲 stdout）。
7. **`config.json` 不进仓库，而它的默认值原本是按开发机写死的**：`api_key` 空着，`live.log`
   指向 `D:/AI/hermes/logs/llama-server.log`。新克隆的副本于是"两个端点都读不到模型"，而页面
   上的报错都看不出问题出在配置上。这个默认值后来改成从 `%LOCALAPPDATA%` 推导 —— **单一路径的
   猜法本身就不可靠**：`%LOCALAPPDATA%` 在任何 Windows 上都有，Hermes 却不一定往里写东西，
   猜错的后果又只是"卡片空白"。现在改成候选列表 + 最新者优先（见 v1.2），key 由
   `tools/sync_hermes_key.py` 在每次启动前同步。
 8. **`for /f` 的命令集里再嵌一层引号，cmd 会解析错**：启动脚本原本用它探测端口，于是每次启动
    都先打印一行 `'C:\...\python.exe" -c "..."' is not recognized`（探测因此永远失败）。
    直接调用、看退出码就够了。
 9. **"端点在线"不等于"取到了数"**：采集器的每一种失败都会变成 `null`，而 `null` 在页面上和
    "服务空闲"长得一样。Strata 那次就是这样 —— `up=True`、模型名、上下文窗口全对，
    `/metrics` 却是 JSON，所有的 counter 都解析成 `None`，**整张模型卡空白而没有任何报错**。
    所以"数不对"要先看**协议**（是不是这种方言），再看字段名。同理，端点的自报状态只说明它答话了。

## 目录结构

```
llm-monitor/
├── monitor.py                 # HTTP 服务 + SSE 推送 + 历史环形缓冲 + 写接口
├── collectors/
│   ├── llama.py               # llama.cpp 指标（router / 单模型自适应）
│   ├── gpu.py                 # nvidia-smi 解析 + 显存归属
│   ├── power.py               # 功耗上限读写 + 按卡推导档位菜单（服务上唯一的写操作）
│   ├── stats.py               # token 账本：counter 折叠 + 原子落盘
│   └── system.py              # Win32 ctypes 采集
├── frontend/
│   ├── index.html
│   ├── app.js                 # 渲染 + Canvas 迷你曲线 + 功耗档位控制
│   └── style.css              # 深浅双主题
├── tools/
│   ├── render_check.js        # 无浏览器的渲染烟测（见下）
│   ├── ledger_check.py        # token 账本的折叠规则自检（见下）
│   └── sync_hermes_key.py     # 把 router 当前的 port / api-key 抄进 config.json
├── config.json
├── stats.json                 # token 账本数据（自动生成）
├── run.log                    # 启动与运行日志（自动生成；pythonw 下唯一可读的输出）
├── start_monitor.vbs          # 普通启动（功耗控制只读）
└── start_monitor_admin.vbs    # 提权启动（解锁功耗控制）
```

### 渲染烟测

前端没有浏览器也能验证：`tools/render_check.js` 用 jsdom 把 `index.html` + `app.js` 跑起来，
喂进真实的 `/api/snapshot` 数据（canvas 用桩替代，CSS 变量从 `style.css` 里解析出来），
然后打印每张卡的标签/数值/脚注、模型标签与明细、表格行数，以及有没有抛异常。
静态检查抓不到"渲染到一半抛错导致白屏"，这个可以。

它同时覆盖两条**不抛异常、但页面就是没用**的失败路径 —— 这两种才是用户真正会发现的：

- **帧契约**：真的连一次 `/api/stream`，确认第 2 帧（稳态帧）里既带 `snapshot` 也带 `point`。
  缺 `snapshot` 就意味着数字会冻住、只有曲线在动。
- **推帧后数字是否真的变了**：推一帧与真实数据不冲突的样本，对比推帧前后 `#gpu` / `#host`
  每张卡的数值；曲线点数还应该 +1（那是 `point` 那条路径）。

它也会检查 **Token 账本**：推帧后账本数字必须跟着变（它同样是只读面板，同样吃帧契约），
并且把 `stats.available` 置为 false 时页面要显示"未启用"，而不是留白或沿用上一帧的旧数。

```bat
set NODE_PATH=%USERPROFILE%\.workbuddy\binaries\node\workspace\node_modules
node tools/render_check.js http://127.0.0.1:8090
```

退出码非 0 = 有未捕获错误，或上面任意一条不成立。**改前端必跑。**

### 账本自检

`tools/ledger_check.py` 直接测 `collectors/stats.py` 的折叠规则，不起服务、不发请求。
账本唯一的职责就是判断"这次读数里有多少是新的"，规则错了页面上只会显示一个看起来很合理的
数字，所以每条规则都单独特判 —— 包括跨重启续算、counter 回退、模型中途上线不算回退、
损坏文件降级、只读路径降级这些手工很难复现的情况。

```bash
python tools/ledger_check.py
```

HTTP 接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 面板页面 |
| GET | `/api/snapshot` | 当前采样 + 历史序列 + 配置（含 `version`、`elevated`、按卡推导的 `power_presets`、`power_settable`） |
| GET | `/api/stream` | SSE，每个采样周期推一帧 `{snapshot, point}`（见「自动刷新是怎么保证的」） |
| POST | `/api/power-limit` | 设置功耗档位，body `{"watts": 140}`（值必须是当前菜单里的一档），需 `X-LLM-Monitor: 1` 头 |

历史保留 900 个采样点（默认 2 秒间隔约 30 分钟），刷新页面不会丢曲线。

## 已知限制

- 系统指标依赖 Win32 API，非 Windows 平台只会返回 `system collector supports Windows only`。
- **WDDM 驱动下 nvidia-smi 不提供每进程显存**，所以进程表里 `llama-server` 的显存常显示
  `N/A`。这里只保留有数据的行和推理进程，避免被一堆桌面进程刷屏。
- CPU 为总负载，不是每核负载（每核利用率需要 PDH 性能计数器，尚未接入）；每核频率是可用的。
- 网络吞吐尚未采集。
- **曲线是手写 canvas，不是图表库**：900 个采样点会先按约 2px 一列分桶并保留每列的极值，
  画成"包络带 + 均值折线"。直接逐点连线在 200px 宽的卡片里会糊成毛线，而且尖峰会被磨平。
- **空闲引擎的瞬时速率是 0**（不产生新的解码耗时），此时卡片退回累计均值并带 `≈` 前缀，
  曲线则显示"暂无数据"——这是引擎真的没干活，不是采集失败。本地模型没在跑的时候
  生成速度/Prefill 两张卡就会长这样。
- y 轴上限是"只升不降得快"的迟滞上限：新峰值立刻抬高上限，之后按 0.88 倍慢慢回落，
  否则曲线每 2 秒会因为上限重算而整体跳一下。
- **功耗档位不是固定的**：`collectors/power.py` 的 `presets_for()` 按显卡自己上报的
  `power.min_limit` / `power.max_limit` 生成菜单 —— 600 W 级卡用标定过的 150/300/400/600 W，
  其它卡按上限的 40/60/80/100% 取整到 5 W。换卡不用改任何代码，面板上的"可用范围 x–y W"
  和按钮会跟着变。前端的中文档位名与错误文案都在 `app.js` 的 `POWER_LABELS` / `POWER_ERRORS`
  里（后端刻意不含中文）。
- **有些卡根本没有软件功耗墙**（笔记本 GPU）：驱动会拒绝 `-pl`，面板识别到之后撤掉档位按钮，
  改为显示驱动实际生效的上限（标「动态」）和出厂默认。此时功耗卡的分母也换成这个生效上限，
  不是留空。
- **token 账本是"面板观察到的增量"，不是引擎自己的账**：面板停机期间如果 server 也重启过，
  那段时间的量会归到重启后的计数上，无法精确切分。首次见到某个模型时，会把它当时已有的
  累计值整体计入 —— 不这么做那部分就永久丢了。跨天归集按**本机日期**切分，不是 UTC。

## 版本记录

版本号写在 `monitor.py` 的 `VERSION`，**全仓只有这一处**，由三条路带出来：启动日志、
`python monitor.py --version`、以及 `/api/snapshot` 的 `config.version`。面板页头的徽章取的
是第三条，所以它显示的一定是**服务端**在跑的版本 —— 浏览器拿了一份旧的 `app.js` 也不会
报出一个前端自己编的号。

### v1.1 — 2026-10-06

**功耗控件不再绑死在 RTX PRO 6000 上。** 换到笔记本 GPU 后暴露出三个问题：档位写死
150/300/400/600 W 而这张卡的窗口是 5–175 W，四个里三个越界；功率读数读 `power.limit`，
而笔记本卡这一列恒为 `[N/A]`，分母为空导致进度条和"上限"都不画；最隐蔽的是驱动拒绝
`-pl` 时**退出码仍然是 0**，只判返回码就会报一次假成功。

- 采样层拆开三个功率字段：`power_requested_w`（软件功耗墙）/ `power_enforced_w`（当前真正
  生效的上限）/ `power_default_w`（出厂默认）；查询失败按三档阶梯回退，缺一列不再拖垮整张卡。
- 档位菜单按卡推导，白名单校验与下发给前端的菜单是**同一份列表**。
- 写入路径三重防护：解析驱动自己的拒绝原文 → 回读校验 → 两条都读不到时如实回
  `verified:false`，不假装确认过。
- 前端：右上角常驻读数「当前 N W · 上限 M W（动态）」，档位名按角色映射，标定过的实测数字
  只在菜单确实来自标定表时才附加显示。

**密钥轮换不再要人工干预。** 新增 `tools/sync_hermes_key.py`：从 router 日志抓当前的
`--port` / `--api-key` 写回 `config.json`（幂等、原子写、输出掩码）。启动脚本在启动前
会先跑一次，hermes 重启换了密钥，双击快捷方式就自动接上，不会再卡在 401。

**另外两处修复**：`DEFAULT_LIVE_LOG` 由写死某台机器的 `D:/AI/hermes/...` 改为从
`%LOCALAPPDATA%` 推导（这个猜法本身不够稳，v1.2 已换成候选列表 + 最新者优先）；当初的两个
`.bat`（现已由 VBS 启动器取代）端口探测里嵌套引号会让 `for /f` 每次都失败、并多打印一行
"系统找不到文件"。

### v1.2 — 2026-10-06

**启动器重写，双击不再留下黑窗口。** 旧的两个 `.bat` 用 `WScript.Shell.Run(cmd, 0)` 隐藏
python 的控制台，而那个窗口样式只对 `conhost` 生效 —— Windows 11 把新控制台默认交给
**Windows Terminal** 托管，样式被忽略。表现是：`git pull` 停掉旧实例后再双击，留下一个
标题为 `monitor`、没有子进程、关不掉的 WT 窗口（关掉服务不会跟着停，因为服务早就不在里头了）。

- `start_monitor.bat` / `start_monitor_admin.bat` 换成 `start_monitor.vbs` /
  `start_monitor_admin.vbs`：入口是 `wscript.exe`，服务是 `pythonw.exe`，全程无控制台。
- 端口探测不再用 `for /f` 跑一次解释器，改用 `WinHttp` 直接请求 `/api/snapshot`
  （不经过系统代理，代理开着也不会误判成"没在跑"而启动第二份）。
- 命令行参数（`--port` / `--interval` 等）原样透传给 `monitor.py`。
- `log()` 现在同时写 `run.log`（超过 1 MB 从头写起），因为 `pythonw.exe` 下 `stdout` 是
  `None`，不落盘的话窗口less 启动失败后没有任何线索。
- `monitor.py` 与 `tools/sync_hermes_key.py` 在入口处给 `None` 的 `stdout` / `stderr`
  补一个 sink：`pythonw.exe` 下 argparse 会在 `main()` 之前就写 stdout 并抛错。

**模型卡片不再静默空白。** 面板"当前模型"一栏的数据全靠 `live` 端点尾随 router 日志；日志
路径一旦猜错，整栏就是空的，而且**页面上没有任何东西说明为什么** —— 看起来和"服务挂了"
一模一样，只有 `run.log` 才知道区别。

- `DEFAULT_LIVE_LOG` 从单一路径改成候选列表（`%LOCALAPPDATA%\hermes\logs\llama-server.log`、
  `D:\AI\hermes\logs\llama-server.log`），由 `resolve_live_log()` 挑：`live.log` 显式配了且
  文件在就听它，否则取存在的候选里**最新的那个**（同时装了 desktop 和 CLI 的机器上，只有正在
  被写入的那份能说出当前模型）。启动日志写明选了哪条、为什么。
- 空白卡片现在会自己解释：日志不在 → 显示它找过的路径；日志在但尾部没有模型请求 → 说清；
  router 报过模型而端口已无人应答 → 直接把模型名和端口写在卡片上。
- 合并逻辑修正：router 与它按需派生的子进程共用一个 `id`，此前"子进程不在"被当成整个来源
  离线 —— 于是健康的 router 显示成 `:57740 离线`，端口还是那个已经退出的进程的。现在任一端口
  应答即在线，名字与端口跟着真正在应答的那个走。
- `tools/sync_hermes_key.py` 改用同一套解析，并且在 **`config.json` 里根本没有 `live` 段**时
  也按有效默认值比对再回写 —— 旧版本写出的 `config.json` 正是这种情况，原先的漏洞恰好会在
  有问题的机器上被跳过。

**端点地址跟着 hermes 走，判据改成直接问 router。** 上面那套修完，卡片还在报**错的**模型：
机器上同时存在两组独立 router（桌面端的和手工起的），而 `config.json` 里钉死的是其中一个的
端口。实测那个端口上跑的模型已经不是面板显示的那个 —— 面板报的是**没人在用**的模型，真正在
服务的那个反而不出现。

- 判据从"读日志尾部的 `proxying request to model X on port N`"改成**问 router 自己的
  `/v1/models`**：`status.value` 是否 `loaded`、`status.args` 里的 `--port` 就是它派生的子进程
  端口。router 只能被问它自己的事，两个 router 不可能互相代答；日志是**按实例**写的，共用一个
  日志文件时最新那行可能属于另一台，这正是误报的来源。日志留作兜底（老的构建、或前面根本没
  router 的裸 llama-server）。
- 地址从 `hermes` 自己的 `config.yaml` 读 `model.base_url`（不引 YAML 库，只手解这一个键），
  文件位置由 live 日志路径推导；找不到就回落 `config.json`，并在启动日志里写明用了哪个。
- **地址每 15 秒重读一次，不可达时立即重读**：hermes 每次启动换端口，只在启动时解析一次的话，
  重启后就再也对不上了。重读只在前端地址真的变化时才下发；下发时同时清掉 router 端点上的
  模型列表/`/props` 缓存与失败退避（换了地址还把退避带过去，会让新地址最多 15 个周期不被采样），
  以及 live 端点里缓存的"哪个模型在跑"的结论。
- 地址里带的 `/v1` 会被去掉再交给采集器：采集器自己拼 `/v1/models`、`/props`、`/metrics`，
  留着后缀会去请求 `/v1/v1/models`，拿回一个 404 —— 而 404 长得和"router 上什么都没加载"一模一样。

**Strata 端点：同一个模型卡片，另一种 `/metrics` 方言。** Hermes 的 provider 换成 strata 之后，
面板上 `strata` 那台**在线、模型名也对，但一个数都没有**：`/metrics` 是 JSON，而采集器按
Prometheus 文本解析，每个 counter 都解析失败 → 恒为 `null`。页面上完全看不出这是协议不匹配，
和"服务挂了"长得一样。

- `collectors/strata.py` 用 JSON 的字段喂出**同一套 counter 名字**，速率仍由 `ModelSampler` 算 ——
  "counter → 瞬时速率"全仓只有一份实现，两种线格式共用。
- 口径按 `serve/server.py` 的定义接：`prompt_ms` 只覆盖**没命中缓存**的那部分（`prompt_n =
  prompt_tokens - cache_n` 就写在同一处），所以 prompt 与 cached 互斥、prefill 只统计前者 ——
  与面板原有口径一致。`kv_hit_ratio` / `mtp_accept_ratio` 因此和 llama.cpp 那套可比。
- **Strata 的两个瞬时速率取自引擎自己报的**（`live.tok_s` / `prefill_tok_s_mean`），不是 counter
  差分：`totals` 是**请求结束时**才累加的（`serve/server.py` 在写 history 那一段加），两次采样之间
  几乎恒为 0，用差分会让曲线永远是空的。累计均值仍由 counter 算，两者在引擎忙时一致。
- `MTP 每步接受` 恒为 `—`：Strata 报草稿"给了多少、收了多少"，不报验证步数，所以既不显示也
  不硬凑。`n_decode_total` 同理。
- 已知限制：`totals` 在服务重启时归零（`ModelSampler`/账本已按"counter 回退 = 重启"处理），但
  同一进程内换模型不会归零 —— 那一段会被记到先加载的那个模型名下。与 llama.cpp 的 counter 同性质。

**一张卡，一份账：按地址去重。** Hermes 直连 Strata 时，`config.yaml` 里的 `model.base_url` 就是
Strata 的地址，于是看板会把它当成两个来源（跟随卡 + 显式配置的 `strata` 卡）。后果不只是画面
重复：账本按 `(endpoint, model)` 记账，两张卡各记一次就是**同一批 token 记两遍**。

- 跟随到的地址若已被别的端点覆盖，就把 hermes 卡与 live「当前模型」卡一起让出来，并在启动日志
  写明 `hermes card hidden: <地址> is already watched as <id>`。live 卡一起让出的理由：它的职责是
  找 router 派生出来的子进程，直连的单模型服务没有子进程可找。
- **每个采样周期重判**，不是启动时定一次：Hermes 随时可能被改回 router（那时地址不同，两张卡
  立刻回来）。
- 地址比较用 host+port，`localhost` 与 `127.0.0.1` 视为同一台；只比字符串会让同一台服务在换一种
  写法后重新裂成两张卡。

**Strata 端点接上，卡不再"在线但空白"。** provider 换成 strata 之后，面板上那台服务模型名都对、
数字一个没有：`/metrics` 是 JSON，而采集器把正文当 Prometheus 文本解析，所有 counter 恒为 `null`。
现在多了一种方言，同一套卡片、同一套速率算法，且按地址去重 —— 详见上文「Strata 端点」与
「一张卡，一份账」两节。

- `collectors/strata.py`：JSON → llama.cpp 的 counter 名字，速率仍由 `ModelSampler` 算。
- `kind` 写进 `config.json` 的端点声明；不写就是 `llama`，老配置不用改。
- 两个瞬时速率改取引擎自报的 `live.tok_s` / `prefill_tok_s_mean`：`totals` 是请求**结束时**
  才累加的，用 counter 差分会让曲线永远是空的。
- 字段映射与地址去重两处都是**静默**失败：读错一个字段、判错一次去重，卡片照常显示一个像样
  的数字或数量，没有任何异常可抓 —— 所以这两处只能拿真实端点逐字段核对验收。

### v1.0

初版。单页面板 + `/api/snapshot`、`/api/stream`、`/api/power-limit` 三个接口，显卡/系统/推理
三路采集，900 点历史曲线，token 账本，以及一条按 600 W 级工作站卡标定的功耗档位条。
