# 财鲸 V3 进销存 —— 本地 Agent

装在**客户自己电脑**上的数据出口：直连本机 SQL Server（财鲸 V3 账套），
把业务能力通过 WebSocket 暴露给云端中转，供 H5 / 小程序 / App 调用。

> 云端**不存任何业务数据**，只做转发。数据始终留在客户机器上。
>
> ```
> H5 / 小程序 / App ──HTTP──► 云端 cloudapi ──WebSocket──► 本地 Agent ──► 财鲸V3 账套
>                              （只转发）      （本仓库）      （SQL Server）
> ```

**当前版本**：`0.2.0`｜打包产物 `V3AgentGui.exe` 约 **33 MB**（免 Python 环境）｜
对外暴露 **61 个可执行操作**。

---

## 一、目录结构

```
进销存-本地Agent源码/
├── v3agent/                    ★ 核心业务包（Agent 的全部能力，约 12.5k 行）
│   ├── main.py                   进程入口
│   ├── agent.py                  WebSocket 客户端（连云端中转 / 帧协议）
│   ├── master.py                 指令分发总控（注册 61 个操作）
│   ├── ops.py                    操作注册表与参数解构
│   │
│   ├── product.py                商品：列表 / 详情 / 改价 / 单位换算
│   ├── sales.py                  销售单：开单 / 改单 / 审核 / 作废 / 复制
│   ├── purchase.py               采购(进货)单：同上
│   ├── inventory.py              库存与出入库流水
│   ├── reports.py                报表：汇总 / 趋势 / 排行 / 应收 / 对账
│   ├── printing.py               单据打印（GDI 直绘，见下）
│   ├── price.py                  取价策略（三级回退）
│   │
│   ├── sql.py                    ★ SQL Server 连接与取号（并发安全）
│   ├── idem.py                   建单幂等闸门（client_token）
│   ├── actor.py                  制单人身份
│   ├── options.py                账套配置项（HxAccountOption 等）
│   ├── auth.py                   账套登录校验
│   ├── selfcheck.py              启动自检
│   ├── op_docs.py                操作说明（给云端的能力清单）
│   │
│   └── yxparts/                  已选配件模块（独立子域）
│       ├── service.py / core.py     业务与内核
│       ├── sqlsource.py             数据源
│       ├── storage.py               本地库（yx_matchlib.db）
│       ├── exporter.py              导出
│       └── ui.py                    界面
│
├── agent_daemon.py             ★ 后台守护进程（真正的常驻者）
├── agent_gui.py                ★ 图形控制台（可随时开关的"遥控器"）
├── setup_gui/setup_app.py         一键安装器界面
│
├── 启动Agent图形界面.bat          ★ 双击启动（推荐）
├── 查看Agent状态.bat               命令行看状态
├── 启动Agent-源码模式.bat          直接用 Python 跑（开发调试）
│
├── 用户文档/
│   ├── Agent使用说明-先看这个.md    给最终用户
│   ├── 装机手册-技术员.md           给实施技术员
│   └── 开通新商户-runbook.md        给运维
│
├── config.template.json        配置模板（打包时带入的空身份模板）
├── requirements.txt            依赖清单
├── *.spec                      PyInstaller 打包配置（GUI / 控制台 / 调试 / 安装器）
├── scripts/                    构建与运维工具（见下）
├── print_formats/              打印格式模板（从账套拉取的 .bin/.txt/.json）
└── docs/                       设计与方案文档
```

---

## 二、架构：为什么关掉窗口 Agent 还在跑

这是本项目最关键的设计。**控制台窗口和 Agent 是两回事**：

```
   V3AgentGui.exe  ──双击──►  ┌──────────────┐
   （控制台窗口）              │  只读遥控器   │  不持有任何连接
                              └──────┬───────┘
                                     │ 点「启动」时 spawn
                                     ▼
                              ┌──────────────┐
                              │ agent_daemon │  ★ 真正持有 WebSocket
                              │  （后台进程） │     独立于窗口存活
                              └──────┬───────┘
                                     │ 写
                                     ▼
                    logs/agent.pid           谁在跑（PID）
                    logs/agent.status.json   在线状态 / 在途计数
                    logs/agent.log           落盘日志
```

**「关窗口」= 关遥控器，不是关 Agent。** 窗口只是轮询 `status.json` 显示状态。

---

## 三、快速开始

### 方式一：用打包好的 exe（推荐）

```
1. 双击「启动Agent图形界面.bat」     → 弹出控制台窗口
2. 窗口里点【启动后台 Agent】         → 状态变「已连接 ✓」
3. ★ 这个窗口现在可以直接关掉         → Agent 继续在后台运行
4. 想停止时，重新双击，点【停止后台 Agent】
```

### 方式二：源码模式（改代码后用）

```bat
pip install -r requirements.txt
copy config.template.json config.json    :: 然后改里面的密钥
python agent_gui.py                       :: 打开控制台
```

### 看状态（不开窗口）

```bat
双击「查看Agent状态.bat」
:: 或
python scripts\status-agent.py
```

---

## 四、配置说明（config.json）

```jsonc
{
  "sql": {
    "server":   "127.0.0.1",
    "port":     1433,
    "user":     "sa",
    "password": "…",                    // 建议改用独立只读账号，别用 sa
    "database": "shiwantongda",         // 目标账套
    "databases": "shiwantongda,tongda", // ★ 允许按请求读的账套白名单
    "tds_version": "7.0"                // SQL Server 2008 R2 必须是 7.0
  },
  "agent": {
    "name":       "shiwantongda",                        // 必须与云端登记一致
    "secret":     "…",                                   // ★ 与云端 agents 密钥一致
    "relay_url":  "ws://…:8000/ws/agent",
    "reconnect_sec": 5,
    "heartbeat_sec": 25,
    "install_id": "…"                                    // 装机唯一标识
  },
  "price": { "default_price_column": "D_SJ1", "recent_price_field": "D_PriceTax" }
}
```

> **★ `config.json` 含密钥，不提交 git**（`.gitignore` 已排除）。
> 打包时带入的是 `config.template.json`（**空身份模板**），所以 exe 同目录必须放一份真实的
> `config.json` 才能启动 —— 这是故意的：避免"从哪个目录双击 exe 就服务哪套账"，
> 也避免把开发者机器的真身份打进包里。

---

## 五、★ 账套可在运行期切换

同一台 SQL Server 上常有多套账。以前换账套要手改 `config.json` 或去另一个目录双击 exe，
两套进程还会共用注册名互相顶号 —— 症状是「数字看着都正常，其实完全是另一套账」。
现在收敛成一个动作：

```bat
python scripts\switch-db.py --list            :: 看有哪些账套、当前是哪个
python scripts\switch-db.py --db tongda       :: 切换（二次确认）
python scripts\switch-db.py --db tongda --yes :: 跳过确认（脚本用）
```

**切换到底发生了什么**（Agent 运行时）：

1. GUI / 脚本只写一个哨兵文件，**不直接改运行态**；
2. 后台 Agent 每 ≤1 秒看一眼，挑「**没有在途请求**」的时刻换库 ——
   每个 op 独立开连接，所以单头与明细不会裂到两套账里；
3. 换完落盘 `config.json`（滚动备份，**只改 `sql.database` 一个键**）；
4. 再用 `SELECT DB_NAME()` **实测**确认，实测值写进 `status.json` 的 `database_verified`。

> 账套列表来自 `sys.databases` 实测 + **业务表四件套**（`XS`/`Hpdan`/`Khdan`/`No_BillID`）判定，
> 所以 `AccMaster`、`TempUpSpRpt` 这类报表引擎库会被自动排除。

---

## 六、★ 并发与一致性（重点）

### 6.1 建单取号：`sp_getapplock` 会话锁

单据号是「读计数 → +1 → 写回」的多步操作，并发下会**撞号**（PK 冲突）。
本实现用 SQL Server 应用锁（`sp_getapplock`）把取号段串行化：

- **不参与行锁图** ⇒ 从机制上**不可能产生 1205 死锁**（这是选它而不是 X 锁的原因）；
- `@LockTimeout` **毫秒级**生效，抢不到就快速失败并明确提示，不长时间阻塞；
- `@LockOwner='Session'` ⇒ 连接断开**自动释放**，不会因崩溃留下死锁。

实测 8 路并发建单：**8/8 成功，单号严格连号、无重复无跳号**。

### 6.2 改单防冲突：`revision` 内容指纹（乐观锁）

两人同时改同一张单，后保存者会**静默覆盖**前者。本实现：

- `bill.get` 返回该单的 `revision`（对"前端会改的字段"取 SHA1 短哈希）；
- `salesbill.update` / `purchase.update` 接受可选 `revision`，与库内当前指纹**不符即拒**
  （`BILL_CONFLICT`），提示「这张单已被他人修改，请重新打开」；
- **不传 `revision` 则完全走原行为** —— V3 桌面端与老前端不受影响（向后兼容）。

### 6.3 幂等：`client_token`

同一 `client_token` 重复到达不会重复建单，直接返回上次那张 ——
挡住「响应丢失后前端重试」造成的重复单据。**不用内容判重**（会误判成"少建单"）。

### 6.4 并发模型

- 快池 `max_workers=8`（列表/明细/单头等轻 op）+ 重池 `max_workers=2`（报表等重 op）；
- 心跳走事件循环内联，不被长任务饿死；
- 云端另有分级在途闸门（report / print / quick）。

---

## 七、打印

打印**不走浏览器出 PDF**，而是 **Windows GDI 直绘**：

- `CreateDCW("WINSPOOL", 打印机名)` 直投，零外部依赖、**不弹对话框**；
- `MM_ANISOTROPIC` 统一成 px@96；文字用 `TA_BASELINE`；线宽 `PS_SOLID,0` = 1 设备像素；
- 打印机列表来自 `EnumPrintersW`（枚举本机真实打印机）；实际打印走系统默认打印机；
- 支持 `_out_file=` 落 PDF/PNG 做逐行逐线比对验收。

打印格式模板在 `print_formats/`（从账套 `HxSysReport` 拉取的 `.bin`）。

---

## 八、scripts/ 工具说明

仓库**只保留构建与运维工具**（验收 / 探针 / 诊断类脚本不发布）：

| 脚本 | 用途 |
|---|---|
| `build_gui.py` | ★ 打包 GUI 版 exe（PyInstaller） |
| `build_installer.py` | 打包一键安装器 |
| `deploy-gui.py` | 部署 exe 到发布目录（含 VERSION.json） |
| `deploy_cloud*.py` / `deploy_python.py` | 云端服务部署 |
| `switch-db.py` | ★ 切换账套 |
| `start-agent.py` / `status-agent.py` | 启动 / 查看状态 |
| `toolchain.py` | 运行时探测（node/python，**不写死版本号**） |
| `sshtarget.py` | SSH 目标与认证的**唯一入口**（本仓 scripts 共用） |
| `rthook_reap_mei.py` | PyInstaller 解包目录回收（运行时钩子） |
| `pull-*.py` / `rmf*.py` | 打印格式拉取与 RMF 模板解析 |
| `run-scheduled.py` | 定时任务 |

> ★ `sshtarget.py` 里**没有任何兜底密码**：凭据只从 env / `~/.ssh/config` / 私钥取，
> 四条认证途径全失败就直接退出。这是刻意的 —— 生产 root 密码曾被复制进 21 个脚本，
> 收敛到一处才是修复。

---

## 九、重新打包

```bat
python scripts\build_gui.py
:: 产物：dist\V3AgentGui.exe
```

构建号 = v3agent 源码树内容的 sha256 前 12 位（内容不变则号不变，不伪装"新版本"）。

---

## 十、常见问题

**Q：双击 exe 没反应？**
窗口渲染需 3–4 秒（tkinter 初始化）。若始终不出，用 `启动Agent-源码模式.bat` 看报错。

**Q：状态显示「已连接」但接口报错？**
看 `logs/agent.log` 尾部。若是 `4001 bad secret`，说明 `config.json` 的 `agent.secret`
与云端登记不一致（**连接被拒 ≠ 网络问题**）。

**Q：接口偶尔 502 / 本地服务正忙？**
- `AGENT_OFFLINE`：Agent 未运行，属正常业务态；
- `AGENT_BUSY`：云端并发闸门限制，按 `Retry-After` 稍后重试。

**Q：同一 Agent 名能跑两份吗？**
**不能。** 云端逻辑是"新连接踢掉旧连接"（`4001 replaced by new connection`），
两份会无限互踢，表现为「接口时好时坏」。清理残留：`python scripts\kill-all-agents.py`
（该脚本属一次性工具，本地保留）。

---

## 十一、隐私与安全

- 本仓库**不含**任何真实密钥；所有配置项以模板形式给出。
- 生产环境的 SSH 凭据、账套口令**不在本仓库**。
