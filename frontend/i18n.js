/* --------------------------------------------------------------------------
 * i18n -- CN/EN toggle, zero framework.
 *
 * The panel grew up Chinese and its strings live in ~260 literals across
 * index.html and app.js. Rewriting every callsite to keys would touch the
 * whole render path and every future string; instead this layer translates
 * the DOM: a longest-first find-and-replace over text nodes and tooltip
 * attributes, run once at startup, on every toggle, and on every node the
 * render loop adds or changes afterwards (MutationObserver).  Chinese keys
 * never occur inside translated English, so one pass is stable and cannot
 * loop.  English text not in the dictionary passes through untouched.
 *
 * app.js uses T() for strings that never enter the DOM (window.confirm).
 * Choice persists in localStorage; default CN, so a fresh check/render pass
 * still sees the Chinese the assertions are written against.
 * ----------------------------------------------------------------------- */
(function () {
  var KEY = 'llm-monitor-lang'; // matches app.js's llm-monitor-theme

  // Chinese -> English. Longest keys first at match time; keep each entry
  // exactly as it appears in source, including punctuation and spacing.
  var DICT = {
    /* brand & top bar */
    'LLM 监控工作台': 'LLM Monitor',
    '正在连接…': 'connecting…',
    '实时推送中': 'live stream connected',
    '推送不可用，已降级为定时轮询': 'push unavailable, fell back to polling',
    '连接断开，正在重连…': 'disconnected, reconnecting…',
    '更新于': 'updated',
    '· 采样': '· sample',
    '各阶段采样耗时：': 'stage timings: ',
    '未上报': 'not reported',
    '显卡': 'GPU', '系统': 'system', '推理': 'engines',
    '深色': 'Dark', '浅色': 'Light',
    '切换到浅色主题': 'switch to light theme',
    '切换到深色主题': 'switch to dark theme',
    '重启': 'Restart',
    '重启 monitor 进程？内存里的曲线历史会被清空，页面随后自动刷新。':
      'Restart the monitor process? In-memory curve history is cleared and the page reloads.',
    '重启中…': 'restarting…',
    '重启被拒绝：': 'restart refused: ',
    '重启超时：新进程没有应答': 'restart timed out: new process never answered',
    '初次加载失败：': 'first load failed: ',
    '脚本错误：': 'script error: ',

    /* section heads & folds */
    '模型信息': 'Models',
    '历史信息': 'History',
    '模型排行 TOP 3': 'Model leaderboard · TOP 3',
    '按功率': 'By power tier',
    '明细': 'Detail',
    '近 7 天汇总': 'Last 7 days',
    '清空数据': 'Clear data',
    '已清空': 'cleared',
    '历史信息已清空': 'history cleared',
    '清空被拒绝：': 'clear refused: ',
    '清空失败：': 'clear failed: ',
    '确认清空？历史数据会永久丢失，无法恢复。':
      'Confirm: history is lost permanently and cannot be recovered.',
    '清空历史信息？今日与累计 token、模型排行、功率档归集、明细表和近 7 天汇总都会归零，并当场写回 stats.json。':
      'Clear all history? Today and total tokens, leaderboards, power-tier totals, detail and 7-day tables all reset and stats.json is rewritten now.',

    /* long header tooltips */
    '推理指标来自 llama-server 的 /metrics、/slots、/props（router 模式会逐个 loaded 模型拉取）；显卡指标来自 nvidia-smi；系统指标来自 Win32 API。曲线可以悬停取值，每张卡片右侧的 i 是该项口径说明，鼠标停在采样时间上可以看各阶段耗时。':
      'Inference metrics come from llama-server /metrics, /slots and /props (router mode polls each loaded model); GPU metrics from nvidia-smi; system metrics from Win32 APIs. Hover a curve to read values; the i on each card explains its definition, and hovering the sample time shows per-stage timings.',
    'llama-server 只把 token 数报成进程生命周期内的累计值，server 重启或 router 卸载模型都会归零。工作台每轮采样把增量折进 stats.json，并连上一次读到的原始计数值一起保存，所以面板自己重启也能接着往下算；面板第一次见到某个模型时，会把它当时已有的累计值整体计入，否则那部分就永远丢了。『今日』按本机日期切分；预填充只统计实际参与计算的 token，走 KV 缓存复用的单独计；数值悬停可看精确值。':
      'llama-server only reports token counts as lifetime totals; a server restart or model unload resets them to zero. Every sample folds the delta into stats.json together with the raw counter it last saw, so the dashboard keeps counting across its own restarts; the first time a model is seen, its existing total is booked in full, or that part would be lost forever. "Today" splits on local dates; prefill counts only tokens actually computed (KV-cache reuse is tallied separately); hover any number for the exact value.',
    '把账本整体归零：今日与累计 token、模型排行、功率档归集、明细表、近 7 天汇总全部清空，并当场写回 stats.json。引擎计数基线会保留，所以清空后不会把 server 的历史累计重新入账；此操作不可撤销。':
      'Zeroes the whole ledger: today and total tokens, leaderboards, power-tier totals, detail and 7-day tables, written back to stats.json immediately. Engine counter baselines are kept, so old server totals are never re-booked. Irreversible.',
    '按账本里每个模型的累计值排名。『输出最多』看累计生成的 token 总数；『输出速度』看引擎自计时口径下的平均生成速率（token ÷ 引擎解码耗时，空闲时间不计入分母），只统计有实测耗时的模型。两者都是自面板开始记账以来的累计值，server 重启不会清零。':
      'Ranked by each model\u2019s cumulative ledger. "Most output" ranks total generated tokens; "output speed" is the engine-timed average decode rate (tokens ÷ engine decode seconds, idle time excluded from the denominator), for models with measured time only. Both are cumulative since the ledger started; server restarts do not reset them.',
    'token 增量在记账的那一刻会被打上当时生效的显卡功耗上限，所以这里回答的是『这些 token 是在哪个功率档下跑出来的』。注意这是按时间归因，不是按用量分摊——功耗上限属于整张显卡，同一张卡上同时跑的几个模型都会记到同一个档位，这个数永远不代表某个模型『用掉了多少瓦』。档位归集从本功能启用那一刻开始记，更早的 token 没有记录当时的档位，所以各档之和小于累计总量；采样时读不到功耗上限（显卡采集关闭或驱动查询失败）会单独记成『档位未知』，不会被并进任何一档。':
      'Token deltas are stamped with the GPU power ceiling in force at the moment they are booked, so this answers "which power tier produced these tokens". It is time attribution, not usage apportionment: the ceiling belongs to the whole card, so every model running on it lands in the same tier, and the number never means "this model burned X watts". Tier accounting starts when the feature is enabled, so tiers sum to less than the total; samples whose ceiling could not be read are tallied separately as "tier unknown".',
    '逐行累计每个『某天 + 某档位 + 某模型』组合的增量。速度用的是引擎自己上报的耗时（llama.cpp 的 prompt_seconds / predicted_seconds 计数器），只在引擎真正计算时增长，所以空闲时间不会把速率摊薄——它反映的是『干活时有多快』。注意预填充速率的分子含 KV 缓存命中的 token，而缓存重读几乎不花时间，所以命中越多这个数越大；对照『KV 复用』一列看，它接近预填充量时说明大部分提示词只是重读缓存而不是真在算。某个组合的耗时计数为 0（例如模型刚加载、该 series 还没导出）时显示为 —。':
      'Per day + tier + model row. Speeds use the engine\u2019s own timers (llama.cpp prompt_seconds / predicted_seconds), which only grow while computing, so idle time never dilutes them -- this reads as "how fast it goes when working". Note the prefill numerator includes KV-cache hits, and cache re-reads cost almost no time, so the higher the hit rate, the larger this number; compare it with the "KV reuse" column: when they match, prompts were mostly replayed from cache rather than computed. A zero timing counter (freshly loaded model, series not exported yet) shows as —.',

    /* token stat cards */
    '今日生成': 'Today · out',
    '今日预填充': 'Today · prefill',
    '累计生成': 'Total · out',
    '累计预填充': 'Total · prefill',
    'decode 调用': 'decode calls',
    ' 次': '',
    // "自 <date> 起" arrives as one text node, so the date cannot be a key of
    // its own: 自 and 起 are matched with their surrounding spaces.  Bare 自
    // used to be translated and turned 自动 into "since动" -- do not shrink
    // these two back to single characters.
    '自 ': 'since ', ' 起': '',
    '尚无记录': 'no records yet',
    '覆盖': 'covers', '天': 'd',
    '账本未落盘（': 'ledger not persisted (',
    '）：当前只记在内存里，工作台重启后会清零。':
      '): living in memory only, it resets when the dashboard restarts.',
    '账本未启用：': 'ledger disabled: ',
    'config.json 里 stats.enabled 设成了 false': 'stats.enabled is false in config.json',
    '本次采样没有上报账本': 'no ledger in this sample',
    '输出 tok': 'output tok',
    '累计生成的 token 总数': 'cumulative generated tokens',
    '生成 t/s': 'gen t/s',
    '引擎自计时口径的平均生成速率': 'engine-timed average decode rate',
    '生成': 'gen', '预填充': 'prefill', '预填充速度': 'prefill speed',
    'KV 复用': 'KV reuse',
    '个档位': ' tiers',
    '档位未知': 'tier unknown',
    '有 ': '', '组记录读不到功耗上限': ' tier(s) have no readable power ceiling',
    '个': '',
    '（显卡采集被关闭或驱动查询失败），已单独计为『档位未知』。':
      ' (GPU collection off or driver query failed) -- tallied as "tier unknown".',
    '日期': 'date', '功率档': 'tier', '模型': 'model', '合计': 'total',

    /* hero & endpoints */
    '当前模型': 'Current model',
    'running': 'running',
    '在线': 'online', '离线': 'offline',
    'spawn 子 server': 'spawned child server',
    '启用': 'enabled',
    '日志文件不存在': 'log file missing',
    '日志是空的': 'log file is empty',
    '日志读不出来': 'log file unreadable',
    '日志尾部没有模型请求记录': 'no model requests in the log tail',
    'router 上没有任何预设处于加载状态': 'no loaded preset on the router',
    'router 没有应答': 'router not answering',
    '原因未知': 'reason unknown',
    '未找到当前模型：': 'current model not found: ',
    // app.js splits this explanation around <code>/<b> tags, so each run of
    // text arrives as its own node and needs its own key.
    'router 已把 ': 'the router lists ',
    '）标记为已加载，但该端口还没应答 —— ': ') as loaded, but nothing answers on its port yet -- ',
    '正在加载中，通常几秒后这里会出现实时指标。':
      'it is still loading; live metrics show up here within seconds.',
    '尚无明细记录。': 'no detail rows yet.',
    '当前服务这个模型的引擎构建版本': 'build of the engine serving this model right now',
    '当前没有已加载的模型 —— 服务在线时指标会自动出现在这里。':
      'No model loaded yet -- metrics appear here as soon as the engine is online.',
    // Also split around <code>: 位 on its own means "digit" and is dropped in
    // the slot cards, so these two must carry 位置 whole or it eats a char.
    '；已询问 ': '; asked ',
    '；查找位置 ': '; looked for ',
    '占已归集生成量的 ': 'of the booked generated tokens ',
    '当前模型来源切换:': 'source switched:',

    /* slot fold */
    'slot 明细': 'slot details',
    '累计均值': 'decode avg',
    'prefill 均值': 'prefill avg',
    '处理中': 'active',
    '请求': 'reqs',
    '已生成': 'Generated',
    '生成速度': 'Decode speed',
    'Prefill 速度': 'Prefill speed',
    '上下文占比': 'Context share',
    '上下文': 'Context',
    'KV 缓存命中': 'KV cache hit',
    'N 个槽 · M 个在跑': 'N slots · M busy',
    '个槽，但引擎这一轮没有上报逐槽数据': ' slots, but the engine reported no per-slot data this round',
    '引擎这一轮没有上报逐槽数据': 'the engine reported no per-slot data this round',
    '配置里有': 'config has',
    '采集失败：': 'collect failed: ',
    '指标采集失败': 'metrics collection failed',
    'slot 采集失败': 'slot collection failed',
    '空闲无请求': 'idle, no request',
    '引擎未上报': 'not reported',
    '未知原因': 'unknown reason',
    '这个 slot 已经生成的 token 数。': 'tokens this slot has generated.',
    '该 slot 自己的生成速度 = 已生成 ÷ 首个 token 之后的秒数，':
      'This slot\u2019s own speed = generated \u00f7 seconds since the first token --',
    '就是社区 tg 那个公式，但**按槽**而非跨槽求和 —— 并发时仍能与单请求基准比较。':
      ' the usual tg formula but **per slot** instead of summed, so it stays comparable to single-request baselines under concurrency.',
    'Strata 只在槽忙时上报；llama.cpp 的 /slots 没有这一项，显示 — 而不是 0。':
      ' Strata reports it only while the slot is busy; llama.cpp /slots has no such field, so show — rather than 0.',
    '血条是该槽当前速度 ÷ 该槽本会话峰值。': 'Bar: this slot\u2019s speed \u00f7 its session peak.',
    '该 slot 读入提示词的速率。引擎只报一个全局 prefill 速率，':
      'How fast this slot reads its prompt. The engine reports a single global prefill rate,',
    '但它一次只读一个提示词（控制行是信号量），所以正在 reading 的那个槽就是这条速率的主人。':
      ' but it reads one prompt at a time (control lines are a semaphore), so whichever slot is reading owns that rate.',
    '没有槽在读、或同时有几个在读时不猜，显示 —。':
      ' Shows — when no slot or several are reading; no guessing.',
    '这个 slot 手上留着的 token 数：在跑时是「提示词 + 已生成」，':
      'Tokens this slot is holding: prompt + generated while running,',
    '空闲时是服务端为下一轮保留的那段前缀。':
      ' while idle it is the prefix the server kept for the next turn.',
    '血条是它占模型上下文上限的比例（下一张卡是同一个数的百分比读法）。':
      ' Bar: share of the model context (the next card shows the same figure as a percent).',
    '该 slot 留着的 token ÷ 模型上下文上限。超过 85% 转红。':
      'Tokens held by this slot \u00f7 model context. Turns red above 85%.',
    '注意这是**单个** slot 的占比，主卡的「上下文占比」取的是各 slot 的最大值。':
      ' Note: this is one **single** slot\u2019s share; the headline "Context share" is the max across slots.',
    '低于 2% 时血条按 2% 画，否则横条上的 0.2% 根本看不见——数字本身没被改。':
      ' Below 2% the bar is drawn as 2%, otherwise 0.2% would be invisible -- the number itself is untouched.',
    '这个 slot 本轮提示词里，前缀缓存直接给回、不需要重新 prefill 的比例。':
      'Share of this slot\u2019s prompt served straight from the prefix cache, skipping prefill.',
    'llama.cpp 的 /slots 直接报这个拆分；Strata 按槽不报，这里取的是「该槽上一轮留下的前缀 ÷ 本轮提示词」——':
      ' llama.cpp /slots reports this split directly; Strata does not report it per slot, so this is "the prefix this slot kept \u00f7 this round\u2019s prompt" --',
    '正是引擎 pick_slot 复用的那一段。主卡的 KV 缓存命中是全局累计口径，两者算法不同，不必相等。':
      ' exactly the stretch pick_slot reuses. The headline KV cache hit is a global cumulative ratio -- different maths, do not expect equality.',
    '本槽占比': 'this slot', '空闲残留': 'idle residue',
    '本轮已出字': 'writing now', '不在读提示词': 'not reading a prompt',
    '并发到 2 个以上时，引擎才把请求装进编号槽并逐槽记账（BGEN）。':
      ' Only at 2+ concurrent requests does the engine book work into numbered slots (BGEN).',
    '只有你一个请求在跑时它走单请求通道（控制行上的 GEN），不落在任何槽上 ——':
      ' A lone request runs on the single-request path (GEN on the control lines) and lands in no slot --',
    '所以每一行的状态都是 idle、速率列空着，这不是采集坏了。':
      ' so every row reads idle and the rate columns stay empty. Collection is fine.',
    '此时上下文列显示的是该槽为下一轮留着的那段前缀。':
      ' The context column then shows the prefix that slot kept for the next turn.',

    /* headline model cards */
    'MTP 命中率': 'MTP accept rate',
    'MTP 每步接受': 'MTP tok/step',
    'MTP 逐位命中': 'MTP per-slot accepts',
    '展开看每一位草稿的接受率': 'expand for per-slot accept rates',
    '投机解码接受率 = 被接受的草稿 token ÷ 草稿 token 总数。':
      'Speculative accept rate = accepted draft tokens \u00f7 all draft tokens.',
    '越高说明 MTP 猜得越准、加速越明显。低于 50% 进度条转黄。':
      ' Higher means MTP guesses better and the speedup is real. Below 50% the bar turns amber.',
    '注意：是否启用 MTP 看的是启动参数里的 --spec-type，':
      ' Note: whether MTP is on follows the --spec-type launch flag,',
    '因为 /props 的 speculative.types 在 MTP 实际运行时也可能报 none。':
      ' because /props speculative.types can report none even while MTP runs.',
    '前缀缓存命中率 = 命中缓存的 token ÷ 全部提示 token。':
      'Prefix cache hit = cached tokens \u00f7 all prompt tokens.',
    '多轮对话里重复前缀越多这个值越高，命中的部分不需要重新 prefill。':
      ' The more repeated prefixes in a conversation, the higher this sits; hits skip prefill.',
    '各 slot 里已用上下文的最大值（提示词 + 已生成）÷ 模型上下文上限。':
      ' Max used context across slots (prompt + generated) \u00f7 model context.',
    '超过 85% 变红，再涨就该清理对话了。': 'Turns red above 85%; time to prune the conversation.',
    'slot 空闲时服务端会清掉提示词计数，此时数字只剩上次请求的已生成残留，':
      ' When a slot idles the server clears the prompt counter, so the number is only the previous request\u2019s residue,',
    '脚注会标出"空闲残留"。': ' and the footnote says so.',
    '每个验证步平均接受几个 token（含那一个必然正确的 token）。':
      'Average tokens accepted per verification step (including the one that must be right).',
    '1.00 表示 MTP 没起作用，接近 2 以上才算真的在加速。':
      ' 1.00 means MTP buys nothing; real speedups live near 2 and above.',
    'Δ已生成 token ÷ Δ解码耗时，分母取引擎自己计时的耗时而不是墙钟，':
      'Δgenerated \u00f7 Δdecode time, the denominator being engine-timed seconds, not wall clock --',
    '否则空闲秒数会被摊进窗口、读数偏低。引擎空闲时不产生新耗时，':
      ' idle seconds would otherwise dilute the window. While idle the engine accumulates no time,',
    '所以数字会退回累计均值并带 ≈ 前缀。':
      ' so the number falls back to the lifetime average with a ≈ prefix.',
    '血条是当前速度 ÷ 本会话峰值速度；空闲时为 0。':
      ' Bar: current speed \u00f7 session peak; 0 when idle.',
    'Δ提示 token ÷ Δ预填充耗时。只在读入提示词时才明显上升；':
      'Δprompt \u00f7 Δprefill time. Only climbs while a prompt is being read;',
    '纯输出阶段它不动是正常的，不是采集失败。':
      ' flat during pure output is normal, not a collection failure.',
    '血条是当前速度 ÷ 本会话峰值速度。': ' Bar: current speed \u00f7 session peak.',
    '均值': 'avg', '· 暂无曲线': '· no curve yet', '· 峰值': '· peak',
    '每步接受': 'tok/step', '已启用': 'enabled', '· 暂无草稿': '· no drafts yet',
    '未启用 MTP': 'MTP off', '累计命中': 'lifetime hit',
    '· 接近溢出': '· near overflow', '· 空闲残留': '· idle residue',
    'tok/步': 'tok/step', '草稿': 'draft', '位': '',
    'MTP 已启用 · 暂无草稿': 'MTP on · no drafts yet', 'MTP 未启用': 'MTP off',
    '暂无草稿': 'no drafts yet',
    'MTP 关闭': 'MTP off',
    '序列峰值': 'seq peak', '批内 slot': 'batch slots', '第': 'No.',
    '/ 上限': '/ limit', '上限': 'limit', '峰值': 'peak',

    /* live state notes & lamps */
    '空闲': 'idle', '读prompt': 'read prompt', '生成中': 'generating',
    '排队': 'queued', '出错': 'error', '等待请求': 'waiting for a request',
    '上次': 'last',
    '引擎自报状态（Strata /metrics live 块）': 'engine-reported state (Strata /metrics live block)',

    /* system cards */
    '显卡利用率': 'GPU util',
    'nvidia-smi 的 utilization.gpu，采样区间内 SM 处于工作状态的时间占比。':
      'nvidia-smi utilization.gpu: share of the sampling window with SMs busy.',
    '批量推理时它常常很低，因为瓶颈在显存带宽而不是算力。':
      ' Often low under batch inference: the bottleneck is memory bandwidth, not compute.',
    '显存占用': 'VRAM',
    '已用显存 ÷ 总显存。脚注里的上限就是这张卡的全部显存。':
      'Used \u00f7 total VRAM. The footnote limit is the card\u2019s full memory.',
    '注意 WDDM 驱动下 nvidia-smi 不给每进程显存。':
      ' Note: under WDDM, nvidia-smi reports no per-process VRAM.',
    '核心温度': 'GPU temp',
    'GPU 核心温度。进度条按 30–90°C 换算占用比例，':
      'GPU core temperature. The bar maps 30-90\u00b0C to 0-100%;',
    '75°C 转黄、83°C 转红并在脚注直接写明温度偏高。':
      ' amber at 75\u00b0C, red at 83\u00b0C with an explicit hot footnote.',
    '功耗': 'Power',
    '当前功耗 ÷ 功耗上限。上限优先取软件功耗墙（nvidia-smi -pl 写进去的那个）；':
      'Current \u00f7 power limit. The limit prefers the software cap (set by nvidia-smi -pl);',
    '笔记本 GPU 根本不上报软件墙，那里显示的是驱动实际生效的上限，':
      ' laptop GPUs report no software cap at all, so the driver-enforced ceiling shows there --',
    '由 Dynamic Boost 在最大 TGP 之内动态调整，所以会在一个区间里浮动。':
      ' Dynamic Boost keeps it moving inside the max TGP.',
    '接近上限说明这张卡已经在全力跑。档位切换在顶栏更新时间左边。':
      ' Near the ceiling means the card is already going all-out. Tier switching lives in the top bar.',
    'CPU 负载': 'CPU load',
    'GetSystemTimes 两次采样差分算出的整机 CPU 占用。':
      'Whole-machine CPU load, differenced from two GetSystemTimes samples.',
    'Windows 的 kernel time 已经包含 idle，所以忙碌比是 (kernel + user − idle) ÷ (kernel + user)。':
      ' Windows kernel time already includes idle, so busy = (kernel + user \u2212 idle) \u00f7 (kernel + user).',
    '这是整机总负载，不是单个核心。': 'This is the whole machine, not one core.',
    '提交内存': 'Commit charge',
    'commit charge = 物理内存 + 页面文件已经"承诺"出去的量，':
      'Commit charge = RAM + pagefile already promised out,',
    '比物理占用更接近真实压力。超过 90% 变红，那时再申请内存就会开始失败。':
      ' closer to real pressure than physical usage. Red above 90%, where new allocations start failing.',
    'MHz · 显存': 'MHz · VRAM', '· 接近上限': '· near limit',
    '温度偏高，注意散热': 'running hot, check cooling',
    '风扇': 'fan', '· 动态': '· dynamic', '上限 — W': 'limit — W',
    '基准': 'stock', '· 负载饱和': '· saturated', '· 压力偏高': '· high pressure',

    /* power tiers */
    '静音': 'Silent', '低耗': 'Eco', '均衡': 'Balanced', '满血': 'Full',
    '复原': 'Stock',
    '最低档：日常长对话的安静选择，解码约为满血的 86%，代价主要在预填充（首字延迟），长提示词尤其。':
      'Lowest tier: the quiet daily driver for long chats; decode keeps ~86% of full speed and the cost lands on prefill (time to first token), especially on long prompts.',
    '低耗': 'Eco',
    '中低档：接近满速、能效最好的区间。解码与满血档通常只差几个百分点，功耗和风扇噪声却低不少。':
      'Low-mid tier: near full speed with the best efficiency. Decode trails the top tier by a few percent, yet draws and sounds noticeably less.',
    '均衡': 'Balanced',
    '中高档：进入曲线平台，再往上加瓦数几乎不涨速——满载实测功耗在这里已经开始抽不满上限，只换时钟顶。':
      'Mid-high tier: the curve plateau — extra watts barely add speed; under load the draw already undershoots this cap, buying only the clock ceiling.',
    '最高档：放开整张卡的功耗上限。预填充最快，代价是功耗、温度和噪声都到顶。':
      'Top tier: full power ceiling. Fastest prefill, at the cost of peak power, heat and noise.',
    '实测：~1386MHz，decode ~61t/s（长上下文），prefill ~10s。':
      ' Measured: ~1386MHz, decode ~61t/s (long context), prefill ~10s.',
    '实测：~2350MHz，decode ~67t/s，峰值速度的 95%，能效最高档（曲线在 ~500W 后变平）。':
      ' Measured: ~2350MHz, decode ~67t/s, 95% of peak speed, best efficiency (the curve flattens past ~500W).',
    '实测：~2755MHz，decode ~70t/s，平台起点；满载平均只抽 ~491W，上限已不是瓶颈。':
      ' Measured: ~2755MHz, decode ~70t/s, the plateau starts here; loaded draw averages only ~491W, so the cap stops being the constraint.',
    '实测：~2777MHz 到时钟顶，decode ~70t/s（长上下文）/159t/s（短）；出厂默认，也就是「复原」。':
      ' Measured: ~2777MHz clock ceiling, decode ~70t/s (long context) / 159t/s (short); factory default, i.e. "Stock".',
    '需要管理员权限': 'admin rights required',
    '不是允许的档位': 'not an allowed tier',
    '超出这张卡允许的范围': 'outside this card\u2019s allowed range',
    '读不到显卡的功耗范围': 'cannot read the GPU power range',
    '这张卡不接受软件功耗墙（驱动拒绝）': 'this card rejects a software power cap (driver refused)',
    '驱动没有按请求生效': 'driver did not apply the request',
    '驱动拒绝了这次设置': 'the driver refused this setting',
    '请求来源校验失败': 'request origin rejected',
    '跨站请求已被拒绝': 'cross-site request refused',
    '缺少必要的请求头': 'required header missing',
    '请求内容不合法': 'bad request body',
    '接口不存在': 'endpoint not found',
    '可用范围': 'allowed range',
    '出厂默认': 'factory default',
    '对应 nvidia-smi -pl 的功率墙。': 'the power cap set via nvidia-smi -pl.',
    '满载时显卡会在此值附近降频，实际功耗不会超过去。':
      ' Under full load the card throttles around this value; real draw stays below it.',
    '需要管理员权限，且重启电脑后恢复默认（可用任务计划程序做开机自动设置）。':
      ' Needs admin rights and resets on reboot (a scheduled task can re-apply it at boot).',
    '出字（解码）阶段卡在显存带宽，适度下调通常几乎不掉速；':
      ' Decoding is bandwidth-bound, so a moderate cut costs almost nothing;',
    '预填充阶段卡在算力，会变慢一些，但它只占一次请求开头的一小段。':
      ' prefill is compute-bound and does slow down, but it is only the opening of a request.',
    '这张卡不上报软件功耗墙（笔记本 GPU 一般由 Dynamic Boost 接管上限），':
      ' This card reports no software power cap (laptop GPUs: Dynamic Boost owns it),',
    '所以读数里的是驱动此刻真正生效的上限，它会随 CPU 负载和温度在最大 TGP 之内浮动，':
      ' so the reading is the ceiling the driver enforces right now, drifting inside the max TGP with CPU load and temperature --',
    '这也是「动态」两个字的来历。': ' hence the "dynamic" tag.',
    '驱动已经明确拒绝过软件设限，所以档位按钮不再提供。':
      ' The driver has refused software caps, so tier buttons are gone.',
    '档位菜单未到，连接恢复后自动补上':
      'tier menu not here yet; it appears once the connection recovers',
    '此卡不接受软件功耗墙，上限由 Dynamic Boost 动态管理':
      'This card takes no software power cap; Dynamic Boost owns the ceiling',
    '当前 X W · 上限 Y W': 'now X W · limit Y W',
    // The tier row's "this one is active" tag is a text node of its own, so it
    // needs a bare key; every longer string containing 当前 wins by length.
    '当前': 'active',
    '当前进程没有管理员权限，改不了功耗上限':
      'This process has no admin rights, so the power cap cannot be changed',
    ' 会被系统拒绝）。请关闭本窗口，改用 ':
      ' (nvidia-smi -pl is rejected by the system). Close this window and relaunch via ',
    ' 启动（双击，会弹一次 UAC 确认）。': ' (double-click; one UAC prompt).',
    '当前进程没有管理员权限，改不了功耗上限（nvidia-smi -pl 会被系统拒绝）。请关闭本窗口，改用 start_monitor_admin.vbs 启动（双击，会弹一次 UAC 确认）。':
      ' This process has no admin rights, so the power cap cannot be changed (nvidia-smi -pl is rejected). Close this window and relaunch via start_monitor_admin.vbs (double-click; one UAC prompt).',
    '当前已经是该档位': 'already on that tier',
    '正在设置…': 'applying…',
    '已设为': 'set to',
    '（驱动不回读软件功耗墙，按请求值显示）': ' (driver gives no readback; showing the requested value)',
    '（原': ' (was', '设置失败（': 'setting failed (',
    '请求失败：': 'request failed: ',
    '显卡功耗': 'GPU power',

    /* ledger helpers */
    '暂无数据': 'no data', '暂无记录。': 'no records.',
    '显卡不可用': 'GPU unavailable', '系统采集不可用': 'system stats unavailable',

    /* TTFT card (2026-10-22) */
    'TTFT': 'TTFT',
    '首 token 延迟 = 引擎实测：从请求进到吐出第一个 token 用了几秒，含排队和读提示词。数值是最近一个已完成请求，脚注给引擎留存样本的平均值与个数。Strata 的 /metrics 请求历史里有这一项；llama.cpp 没有，显示 —。':
      'Time to first token, measured by the engine: seconds from the request arriving to the first token coming out, queue and prompt read included. The number is the most recently completed request; the footnote gives the mean and count over the engine\'s retained samples. Strata\'s /metrics request history carries it; llama.cpp has none, so show —.',
    ' 个请求': ' requests',
    '暂无已完成请求': 'no completed request yet',
    '暂无每步数据': 'no per-step data yet',

    /* ledger view tabs (2026-10-23) */
    '模型排行': 'Ranking', '运行功率': 'Power',
    'token明细': 'token detail', '7日汇总': '7 days',
    /* energy pane (2026-10-24) */
    '能耗面板': 'Energy',
    '显卡功率 · 近 30 分钟': 'GPU power · last 30 min',
    'nvidia-smi 实测 · 悬停看精确值': 'measured via nvidia-smi · hover for exact values',
    '每日能耗': 'Daily energy',
    '实测 GPU': 'GPU measured', '估算整机': 'machine est.',
    '显卡功率': 'GPU power', '整机功率': 'Machine power',
    '墙插功率': 'Wall power', '今日电费': 'Today cost',
    '估算': 'est.',
    '实测 · 今日 ': 'measured · today ', '实测 · 今日暂无': 'measured · nothing today',
    'CPU 估算 + 平台 ': 'CPU est. + platform ',
    '按电源效率 ': 'at PSU efficiency ',
    '电价假设 ': 'price assumed ', '元/kWh': 'CNY/kWh',
    'GPU 实测': 'GPU measured', '整机估算': 'machine est.',
    '墙插估算': 'wall est.', '电费估算': 'cost est.',
    '账本未启用：没有能耗积分。': 'ledger off: no energy books.',
    '能耗自 ': 'energy booked since ',
    ' 起按采样积分；采样覆盖的时长见明细表『采样』列。':
      ' per-sample; the Sampled column shows coverage.',
    '开始记账后每轮采样自动积分，暂无数据。':
      'energy integrates every sample once the books start; no data yet.',
    'nvidia-smi 实时读数': 'live nvidia-smi reading',
    '全部显卡实时功耗之和，nvidia-smi 读数':
      'sum of all cards, live nvidia-smi readings',
    '采样': 'Sampled',
    '峰 ': 'peak ', ' / 谷 ': ' / valley ', '元/kWh · 按小时': ' CNY/kWh · hourly',
    '本日含无小时数据的电量，按一口价补算': 'includes pre-TOU energy billed at the flat price',
    '带＊的日期含无小时数据的电量，该部分按一口价 ': '(* the marked dates include pre-TOU energy priced flat at ',
    ' 元/kWh 补算。': ' CNY/kWh.)',
    '把每 2 秒采样的功率积分成电量。GPU 瓦数是 nvidia-smi 实测；『整机』『墙插』『电费』是估算——Windows 上没有 Linux RAPL 那套逐核功耗遥测，CPU 瓦按占用率线性模型估（15 W 空闲 + 最高 65 W），平台（主板/内存/SSD/风扇）记固定 130 W，电源效率按 90% 折算到墙插。电费按 config（stats.tou）的峰谷价逐小时积分；启用峰谷之前记的账没有小时拆分，按一口价 0.60 元/kWh 兜底并在表里标＊。能耗只覆盖工作台在采样的小时（明细表里有每天采样时长），机器关机或面板没跑的时间不计。':
      'Watts are integrated every 2 s sample. GPU watts are measured by nvidia-smi; machine, wall and cost are estimates — Windows has no RAPL-style per-core power telemetry, so CPU watts use a flat load model (15 W idle + up to 65 W), the platform (board/RAM/SSD/fans) is a fixed 130 W, and the PSU is assumed 90 % efficient. Cost is billed hour by hour against the peak/valley schedule in config (stats.tou); days booked before TOU started have no hourly split and fall back to a flat 0.60 CNY/kWh, marked ＊ in the table. Energy covers only the hours this dashboard was sampling (see the Sampled column); shutdown and idle time with the panel off are not booked.',
    '把每 2 秒采样的功率积分成电量。GPU 瓦数是 nvidia-smi 实测；『整机』『墙插』『电费』是估算——Windows 上没有 Linux RAPL 那套逐核功耗遥测，CPU 瓦按占用率线性模型估（15 W 空闲 + 最高 65 W），平台（主板/内存/SSD/风扇）记固定 130 W，电源效率按 90% 折算到墙插，电价按 0.60 元/kWh 假设。能耗只覆盖工作台在采样的小时（明细表里有每天采样时长），机器关机或面板没跑的时间不计。':
      'Watts are integrated every 2 s sample. GPU watts are measured by nvidia-smi; machine, wall and cost are estimates — Windows has no RAPL-style per-core power telemetry, so CPU watts use a flat load model (15 W idle + up to 65 W), the platform (board/RAM/SSD/fans) is a fixed 130 W, the PSU is assumed 90 % efficient and the tariff is an assumed 0.60 CNY/kWh. Energy covers only the hours this dashboard was sampling (see the Sampled column); shutdown and idle time with the panel off are not booked.',
  };

  /* ------------------------------------------------------------------ core */

  // NOTE: do NOT rebuild the lookup as one 266-branch alternation RegExp. V8
  // chokes on it (renderer hits a 128 MB allocation and dies, in jsdom and
  // Chrome alike). Sorted split/join passes are plenty fast for text nodes,
  // longest key first so compound terms win before their parts.
  var CN_KEYS = Object.keys(DICT).sort(function (a, b) { return b.length - a.length; });

  function tr(cn) {
    for (var i = 0; i < CN_KEYS.length; i++) {
      var k = CN_KEYS[i];
      if (cn.indexOf(k) !== -1) cn = cn.split(k).join(DICT[k]);
    }
    // A replacement that starts with a space (" 个档位" -> " tiers") lands next
    // to the space already in the source and leaves "3  tiers".  Two spaces
    // never mean anything in this UI, so collapse them; no trim, because
    // several values deliberately begin with one.
    if (cn.indexOf('  ') !== -1) cn = cn.replace(/ {2,}/g, ' ');
    return cn;
  }

  // One-way translation with a memory: the panel's source of truth is always
  // Chinese (app.js renders Chinese literals), so instead of translating
  // backwards we snapshot each node's Chinese text and RESTORE it when the
  // user toggles back. English strings that were never Chinese (model names,
  // 'Strata 0.1.45', 'read prompt') never enter the snapshot and never move.
  var CJK = /[\u4e00-\u9fff]/;
  var SNAP_TEXT = new WeakMap();   // text node -> its Chinese source text
  var SNAP_ATTR = new WeakMap();   // element   -> {attr: Chinese source text}
  var ATTRS = ['data-tip', 'title', 'placeholder'];

  var lang = 'cn';
  try { lang = localStorage.getItem(KEY) || 'cn'; } catch (e) {}
  var busy = false;

  function skip(node) {
    var el = node.nodeType === 3 ? node.parentElement : node;
    for (; el; el = el.parentElement) {
      if (el.id === 'lang-btn') return true; // its label is hand-written per lang
    }
    return false;
  }

  function visitText(node) {
    var t = node.nodeValue;
    var p = node.parentElement ? node.parentElement.tagName : '';
    if (p === 'SCRIPT' || p === 'STYLE') return;
    if (skip(node)) return;
    if (CJK.test(t)) {
      SNAP_TEXT.set(node, t);
      if (lang === 'en') {
        var en = tr(t);
        if (en !== t) node.nodeValue = en;
      }
      return;
    }
    // Latin-only text while in CN: it may be our own earlier English pass.
    var orig = SNAP_TEXT.get(node);
    if (lang === 'cn' && orig != null && CJK.test(orig) && t !== orig) {
      node.nodeValue = orig;
    }
  }

  function visitAttr(el, name) {
    if (el.nodeType !== 1 || skip(el)) return;
    var v = el.getAttribute(name);
    if (v == null) return;
    var m = SNAP_ATTR.get(el);
    if (!m) { m = {}; SNAP_ATTR.set(el, m); }
    if (CJK.test(v)) {
      m[name] = v;
      if (lang === 'en') {
        var en = tr(v);
        if (en !== v) el.setAttribute(name, en);
      }
    } else if (lang === 'cn' && m[name] != null && CJK.test(m[name]) && v !== m[name]) {
      el.setAttribute(name, m[name]);
    }
  }

  function walk(root) {
    // No busy check here on purpose: apply() walks WITH busy=true (it is the
    // writer the guard protects against, not a victim of it).
    if (!root || (root.nodeType !== 3 && !root.querySelectorAll)) return;
    if (root.nodeType === 3) { visitText(root); return; }
    if (!root.querySelectorAll) return;
    var walker = document.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
    for (var n; (n = walker.nextNode());) visitText(n);
    ATTRS.forEach(function (name) {
      visitAttr(root, name);
      var els = root.querySelectorAll('[' + name + ']');
      for (var i = 0; i < els.length; i++) visitAttr(els[i], name);
    });
  }

  function paintButton() {
    var btn = document.getElementById('lang-btn');
    if (!btn) return;
    btn.textContent = lang === 'en' ? '中文' : 'EN';
    btn.title = lang === 'en'
      ? 'Switch back to Chinese'
      : '切换为英文界面（仅面板文案，README 保持中文）';
  }

  function apply() {
    busy = true;
    try {
      document.documentElement.classList.toggle('lang-en', lang === 'en');
      walk(document.body);
      paintButton();
      // Canvas labels are painted, not walked, so the observer cannot reach
      // them: a card with an empty series keeps "暂无数据" in Chinese until
      // something rebuilds that canvas. app.js listens for this and repaints
      // from the snapshot it already holds. That repaint writes the Chinese
      // source back into the DOM, so the walk runs a second time -- and both
      // the dispatch and the second walk happen with busy still set, which is
      // what keeps the repainted text from flickering in Chinese for a beat.
      try { window.dispatchEvent(new Event('i18n-change')); } catch (e) {}
      walk(document.body);
      paintButton();
    } finally {
      busy = false;
    }
  }

  // Late-arriving DOM (the render loop rebuilds cards every tick): each added
  // or changed node goes through the same snapshot-or-translate step. Re-entry
  // from our own writes is harmless: a node is only written when it already
  // differs from its snapshot, and the replacement text never re-matches the
  // trigger on the next pass.
  var observer = new MutationObserver(function (muts) {
    if (busy) return;
    muts.forEach(function (m) {
      if (m.type === 'characterData') { visitText(m.target); return; }
      Array.prototype.forEach.call(m.addedNodes || [], function (n) {
        if (n.nodeType === 1 || n.nodeType === 3) walk(n);
      });
      if (m.type === 'attributes') visitAttr(m.target, m.attributeName);
    });
  });

  function boot() {
    try {
      if (/[?&]nolang=1/.test(location.search)) return; // debug bypass
    } catch (e) {}
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributeFilter: ATTRS, attributes: true,
    });
    apply();
    var btn = document.getElementById('lang-btn');
    if (btn) btn.addEventListener('click', function () {
      lang = lang === 'en' ? 'cn' : 'en';
      try { localStorage.setItem(KEY, lang); } catch (e) {}
      apply();
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }

  // app.js asks through T() for strings that never touch the DOM
  // (window.confirm and friends).
  window.I18N = {
    t: function (s) { return lang === 'en' ? tr(s) : s; },
    get lang() { return lang; },
  };
  window.T = window.I18N.t;
})();
