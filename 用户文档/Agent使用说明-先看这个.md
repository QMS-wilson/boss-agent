# V3 本地 Agent —— 使用说明

> 2026-09-19 更新。**先看这一页，不要再翻旧的那些 .bat 了。**

---

## 一句话

**双击 `dist\V3AgentGui.exe` → 点【启动后台 Agent】→ 关掉窗口。**

Agent 会继续在后台跑，小程序照常能用。

---

## 为什么可以关窗口

窗口只是**遥控器**，不是 Agent 本身。

```
   ┌─────────────────────┐               ┌──────────────────────────┐
   │  V3AgentGui.exe     │  读状态        │  AgentDaemon（后台进程）   │
   │  控制台窗口          │ ───────────>  │  ← 真正连云端的是它        │
   │  想开就开，想关就关    │               │  ← 开机后一直跑            │
   │                     │  点「启动/停止」 │                          │
   │                     │ ───────────>  │  logs\agent.status.json   │
   └─────────────────────┘               │  logs\agent.pid           │
             关掉 ✗                       │  logs\agent.log           │
              ↓                          └──────────────────────────┘
         Agent 不受影响 ✓
```

所以：

| 你做什么 | Agent 会怎样 |
|---|---|
| 关掉窗口 | **继续跑** ✓ |
| 再打开窗口 | 看到它还在线、跑了多久、收发了多少 |
| 点【停止后台 Agent】 | 才真正停掉 |
| 重启电脑 | 停掉（需要手动再点一次启动） |

---

## 窗口里能看到什么

```
 V3 本地 Agent 控制台                              后台运行中     ← 标题栏状态
 后台 Agent：运行中（PID 16432）                                  ← 进程是否活着
 配置：D:\v3-wechat\config.json   Agent 名：shiwantongda
 云端：已连接 ✓
 接收 0 条 / 发送 12 条 / 运行 3 分 20 秒   （Agent 版本 0.1.0，更新于 22:41:03）

 [启动后台 Agent] [停止后台 Agent] [刷新状态] [查看日志文件] [打开配置文件]

 ─────────────────────────────────────────────────────────────
 [22:41:01] → 请求 product.search
 [22:41:01] ← 完成 product.search  218ms total=48586      ← 每条请求都有耗时和结果
 ─────────────────────────────────────────────────────────────
```

★ 窗口里的日志是**后台 Agent 写的**，不是窗口自己产生的。
所以**关掉窗口再打开，日志还在**（存在 `logs\agent.log`）。

---

## 为什么之前「双击 V3Agent.exe 没反应」

`dist\V3Agent.exe` 是 `console=False` 打包的，PyInstaller 不给它开窗口。
**进程其实起来了，但你什么都看不到** → 你以为没反应 → 反复双击 → 叠出 8 个进程。

而云端**每个 Agent 名字只允许一条连接**（`cloudapi/registry.py` 里
「新连接顶掉旧连接」的逻辑）。多个进程互相抢，结果是：

```
received 4001 (private use) replaced by new connection
```

无限互踢，**一个都连不稳**，接口时好时坏。

> **`V3Agent.exe` 已经不建议用了。** 新架构下：
> - 控制台点【启动】时，会**自动清掉所有残留 Agent**
> - 只有**一个**后台进程持有云端连接，不会再互踢

---

## 今天到底修了什么

| 问题 | 根因 | 修法 |
|---|---|---|
| 列表分页不生效 | 跑的是 **09-11 打包的旧 exe**，改 `.py` 对它零影响 | 重新打包，已含 `ROW_NUMBER()` 分页 |
| Agent 连接反复掉 | 本机开着**多个 Agent 实例抢唯一连接** | 启动时自动清理，只保留一个 |
| 双击「没反应」 | 打包成无窗口 exe，进程默默在跑 | 改用有窗口的控制台 |
| 关窗口就断线 | Agent 跑在 GUI 进程里，窗口一关就死 | **Agent 独立成后台进程**，与控制台解耦 |

---

## 验证过了吗

验过了。2026-09-19 核心验收 `scripts\verify-detach.py`：

```
[1] 启动后台 Agent          -> 后台 Agent 已启动（PID 16432）
[2] 等云端显示在线          -> t=1s online=True ops=29
[3] 模拟用户关窗口          -> 杀掉 GUI，保留后台 PID 16432
[4] 关窗口后持续观察 15 秒  -> 进程存活=True  云端在线=True  状态文件=已连接  ×5 次
[5] 关窗口后调接口          -> products total=48586 hasMore=True  ✓
[6] 点停止                  -> 已停止：PID 16432   停止后 云端在线=False
结论：★★★ 通过 —— 关窗口后 Agent 仍在线 ✓
```

复现命令：

```bat
python scripts\verify-detach.py
```

---

## 出问题怎么办

**1. 窗口显示「未运行」，但小程序还能用**

说明有后台 Agent 在跑，只是 pid 文件丢了。点【刷新状态】多等 2 秒；
还是不行就点【停止后台 Agent】再【启动】。

**2. 一直「连接中…」或反复重连**

点【停止后台 Agent】→【启动后台 Agent】。启动时会自动清理残留。

命令行排查（看是谁在占用云端连接）：

```bat
python scripts\who-is-the-agent.py
```

**3. 想彻底清理本机所有 Agent**

```bat
python scripts\kill-all-agents.py
```

**4. 云端确认（登录云服务器 149.88.75.125）**

```bash
journalctl -u v3cloud --since "10 min ago" | grep "Agent 上线" | wc -l
```

正常值：**你自己开几个就是几**（一般 1）。
如果几十上百，就是又有进程在抢了。

---

## 查看日志

后台 Agent 的日志在：**`D:\v3-wechat\logs\agent.log`**

窗口顶部的【查看日志文件】按钮可以直接打开。
关掉窗口后 Agent 产生的日志也会写进去，不会丢。

---

## 给别的客户电脑移植

两个文件就够：

```
V3AgentGui.exe          ← 从 dist\ 拿
config.json             ← 放 exe 同目录，改 SQL / 云端地址 / 密钥
```

`config.json` 要改的：

```json
{
  "sql":   { "server": "127.0.0.1", "user": "sa", "password": "对方的SQL密码",
             "database": "对方的库名" },
  "agent": { "name": "对方的Agent名", "secret": "对方的密钥",
             "relay_url": "ws://149.88.75.125:8000/ws/agent" }
}
```

> 注意：对方电脑要有 `D:\v3-wechat\logs\` 可写（程序会自动建）。
> 如果装在别的盘，启动时设环境变量 `V3AGENT_RUNTIME_DIR` 指向可写目录。

---

## 常用命令

| 想干什么 | 命令 |
|---|---|
| 重新打包控制台 | `python scripts\build_gui.py` |
| **验收：关窗口不停 Agent** | `python scripts\verify-detach.py` |
| 查谁在抢连接 | `python scripts\who-is-the-agent.py` |
| 清理全部 Agent 进程 | `python scripts\kill-all-agents.py` |
| 源码模式开控制台 | `启动Agent-源码模式.bat` |

> ★ 所有 `python` 指 **系统 Python 3.13**：
> `C:\Users\24773\AppData\Local\Programs\Python\Python313\python.exe`

---

## 已知待办（不影响使用）

- `shiwang` 这个账号的密码该换了
- 云端 SSH 还是密码登录，建议换密钥 + 上 HTTPS
- 开机自启还没配（重启后需要手动点一次启动）
- `access: 'guest'` 的写权限没验证过，要测写入前先确认
