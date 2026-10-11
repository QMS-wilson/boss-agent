# 开通新商户 runbook（平台方 · 2026-09-26 16:30 重写）

面向"平台运营方给第 N 家商户接入"。**顺序不能跳**，每一步后面写的都是"这步没做会出什么事"。
现场/远程装机配合的部分在 `装机手册-技术员.md`，这份只管平台侧。

> 示例里的商户码一律写 `<码>`。历史上这份文档用 `yedian` 做例子，而那个测试商户已经删了
> （15:37 从后台按"账号→客户端→商户"三道门删干净），照旧例子跑会撞 404。

前置条件（缺就先补，不要往下走）
- 该商户自己的机器上有财鲸 V3 + SQL Server 账套，且知道库名（`sql.database` 要填它）。
  一台机器**几本账都行**：装机前用 `scripts\switch-db.py --list` 或安装器的「现场自检」把清单打出来。
- 云端可 SSH（走 `scripts/sshtarget.py`，密钥 `~/.ssh/jxc_deploy`；**不要**再往任何文件里写 root 密码）。
- 后台密码只在服务器上的 `cloud_config.json` 里；agent 登不进线上后台，验收用 `check-admin-live.py`。

## 1. 开通（后台向导或一条命令，二选一）

**推荐后台**：`http://149.88.75.125:8000/admin` → 「商户」→ 开通向导 4 步
（预检 → 出包"没勾完三项不放行" → 等首连 → 第 4 步只给该跑的发布命令、**不代跑**）。
密钥/令牌/初始口令**只在出包面板显示一次**，关掉页面就没了，当场抄进台账。

命令行等价物：

```bat
<venvPy> 进销存-云端后台源码\scripts\provision-merchant.py --merchant <码> --title "<展示名>" ^
         --access auth --database-hint <该商户账套名> --from-ssh --dry-run
<venvPy> 同上，去掉 --dry-run
```
它按顺序建 **商户实体 → 同名 Agent（密钥服务端生成，只回显一次）→ 客户端 → 首个应用账号**，
出包写到仓库外 `~/.jxc-provision/merchant-bundle-<码>.json`。

- 商户码规则 `^[a-z][a-z0-9-]{1,29}$`，**商户码 == Agent 名 == H5 URL 前缀**；
  后台、安装器、前端、nginx 四处同一套（`scripts/verify-installer.py` 会逐条比对，改任何一处都要重跑）。
  保留字 `admin/static/assets/api/ws/logs/index/h5/web` 不能当码 —— 撞了会遮掉入口。
- 名字已被占用 → 退出码 2 并停下。这是刻意的：`POST /api/admin/agents` 历史上会静默换密钥，
  名字打错一个字母就把在线那家锁死。
- 出包文件不要提交、不要贴进群。

## 2. 交付装机包（客户机）

把 `dist\V3Agent一键安装.exe`（**不是** Agent 本体 exe）交给现场/远程的人，
连同三项：商户码、Agent 密钥、该商户账套名 + SQL 账号口令。口令走电话/密码管理器，不发群。

- 安装器只装 `V3AgentGui.exe` 这一份，并把开机自启写成
  `"C:\V3Agent\V3AgentGui.exe" --__daemon --config "C:\V3Agent\config.json"`。
  ★ 少任何一段都是"任务注册成功、重启后集体掉线"：裸跑开的是窗口、不连云端，
  而 ONSTART 的工作目录不是安装目录 ⇒ 找不到 config（P1-4 后不再兜默认身份）。
- 安装包/模板里**没有任何默认身份**：`agent.name`/`sql.database`/`sql.user`/`sql.password`
  少填一个，Agent 直接拒绝启动并说清缺哪个 —— 堵的就是"用 A 家的名字读 B 家的账"（实测发生过，跑了 2 小时才发现）。
- **不要把装好的目录整份拷给另一台机器**：带着同一个 `install_id` 上线，云端按
  「同指纹不同机」`close(4002)` 拒掉后来者，症状是"客户说打不开"，最难查。
- 一台机器可以有**多份安装**（不同目录、不同任务名 `V3Agent-<码>`，进程互不干涉，
  清理/停止只按各自目录 —— `scripts/verify-multi-agent-host.py` 锁这条）。
  但**一个商户码仍然只能有一份在跑**：云端同名只留一条连接。

### 2b. 一台主机两本账（Step 2，2026-09-26 起支持）

一位老板一台服务器上装着两本账时，**放行清单有两份，都要登记**，少一份的症状完全不同：

| 登记处 | 怎么登记 | 不登记的后果 |
|---|---|---|
| 云端「这家允许读哪几本」 | 新后台 → 商户 → 详情里的账套清单（或 `provision-merchant.py --databases "A,B"`） | 请求层面进不去那本（Step 3 之后会明确拒），后台也不会为它报警 |
| 客户机「这个进程允许被读到哪几本」 | 在安装目录跑 `scripts\switch-db.py --allow "A,B"` | 请求到了客户机被 `DATABASE_NOT_ALLOWED` 挡回；**现场自检会直接点名并给出这条命令** |

### 2c. 请求点名读哪本：平台方调试台与老板手机上都能用了（Step 3 + Step 4）

- 客户端请求可带 **`X-Database: <账套名>`**（REST `/api/v1/*`、信封式 `/api/v1/rpc`、异步任务
  三条链都认）。**不带就是默认库** ⇒ 现网那份 H5/小程序行为一个字节都没变。
- 越界在**云端**就挡下（`403 DATABASE_NOT_ALLOWED`），不把请求转给客户机去拒；
  审计里记下"谁试着读哪本"（`database` 字段，被拒也记）。
- ★ **客户机是旧包时点名账套 ⇒ `502 AGENT_NO_DB_ROUTING`**，不会"那就读默认那本"。
  静默读错账比直接失败严重得多。所以"换包"（`0.2.0+c1bf3af4a0a9` 起）与"登记清单"
  **两件事都要做**，只做一件的现象就是"点第二本报错"。
- 平台方现在就能用：后台 → 设置 → 调试工具，选完商户后**旁边那个账套下拉**就是，
  结果头部写"点名 X ｜ 实际读到 Y"。
- 老板手机上：入口**只在云端登记了 2 本以上时才出现**（首页/「我的」里的"切账套"）。
  ★ 一旦是两本以上，手机发出的每个请求都**明确带库名**（含默认那本），所以客户机上做过
  热切换也不会串账；只登记一本的商户永远不带这个头，行为与改造前逐字节一致。
  登录响应里 `databases` 的第一项就是手机上那本"默认"，云端会按客户机**实测**那本轮转它
  （客户机离线时保持登记顺序）—— 所以登记时把在用的那本填第一行最稳。

- 两份清单的差集在后台看得见：商户行会出现 warn
  「云端允许这家读「A、B」，但那台客户机只放行「A」—— …跑 `switch-db.py --allow "A,B"`」。
- **默认库只有一处定义**：`config.json` 里的 `sql.database`（= 不带库名时读的那本）。
  清单第一项永远跟着它，不会因为登记顺序把"默认"变成两本里的另一本。
- 登记清单**不是切库**：`--allow` 只改 `sql.databases` 一个键，当前在读哪本仍由热切换/GUI 决定。
- 客户机改完清单**不用重启**：常驻 Agent 每秒比对 config.json 的修改时间，只同步这一个键
  （刻意不同步 `sql.database`/密钥，否则会出现"热切被配置文件改回去"）。
- 越界定义：`DATABASE_NOT_ALLOWED`（本机没放行）、`DATABASE_NOT_FOUND`（本机没这本库）、
  `DATABASE_NOT_ACCOUNT_SET`（库在但不是财鲸V3 账套，缺哪些表会点名）。
  ★ 老 config（没写 `sql.databases`）**行为完全不变**：只允许读 `sql.database` 那一本。

## 3. 核对账套（唯一可信的是实测值）

```bat
type <安装目录>\logs\agent.status.json
```
看 **`database_verified`**（Agent 自己 `SELECT DB_NAME()` 实测的），不要看 `database`
（那只是配置里的期望值）。或者让对方在安装器/控制台账套里点「现场自检」，把
`logs\selfcheck-<时间>.txt` 发回来 —— 那份文件里连**应收合计与客户数**都有，
拿去和 V3 客户端对一遍，接错账套当场就能发现。

## 4. 确认在线（一家在线不代表没事）

后台 → 「商户」：每行第一列就是**实测账套**（B3 上报），与商户码不一致直接进告警条。
再看 版本 / op 数 / 装机指纹 / 主机名。

- 出现 `同名接入被拦：<码>` = 有第二台机器拿着同一份密钥要上线。**在线那家不会被顶掉**（P1-3）。
  确实是换机/重装 → 点该行「接管」放行 3 分钟，让对方重连；不点就一直被拒。
- 云端日志速查：`journalctl -u v3cloud --since "10 min ago" | grep "Agent 上线"`（打 `install=<指纹> host=<机器名>`）。

## 5. 发布这家 H5 前缀

```bat
<venvPy> 进销存-uniapp\scripts\deploy.py --merchant <码> --no-build --stage   :: 只上传+建链，现网零变化
<venvPy> 进销存-uniapp\scripts\deploy.py --merchant <码> --no-build          :: 确认后切 nginx
<venvPy> 进销存-uniapp\scripts\deploy.py --sync-state                        :: 把发布矩阵回写给后台
<venvPy> 进销存-uniapp\scripts\check-deploy-matrix.py --merchants <码>,shiwantongda  :: 必须全绿（当前 39/39）
```
★ `check-deploy-matrix.py` **裸跑不通**（"✗ 没拿到商户码"），必须带 `--merchants`（逗号分隔，含现存的 `shiwantongda`）。
一份共享构建 + 每商户一条软链；重复跑是 no-op（同构建跳过上传、conf 未变不 reload）。
白屏的唯一有效判据是页内 `.js` 的 Content-Type 与体积 >100KB，**不是** HTML 相同或 200。

## 6. 负向测试（这一步做完才算真验完）

```bat
<venvPy> 进销存-云端后台源码\scripts\check-tenancy-live.py       :: 当前 13/13
<py313>  进销存-云端后台源码\scripts\check-admin-live.py         :: 当前 41/41（线上只读，密码不出服务器）
<py313>  进销存-uniapp\scripts\probe-api.py                      :: 应收合计应与该账套自检文件里的数一致
```
隔离本身三条：本商户账号点名别家 → `403 TENANT_MISMATCH`；未知商户码 → `404`；
未升级客户端在"只有一家授权"时不带 `X-Merchant` 仍可用。

再手动看一眼浏览器（唯一能证明"不串号"的判据）：
- 用 A 家已登录的浏览器开 `http://149.88.75.125/<B码>/` → **必须是登录页**，不得出现 A 家任何数字；
- 存储键应是 `jxc_auth:<B码>`，A 家是 `jxc_auth:<A码>`，根站裸键 `jxc_auth` 保留不动。

## 7. 回退

| 情况 | 动作 |
|---|---|
| 这家前缀不该露出来 | `deploy.py --merchant <码> --unpublish`（只删软链，立刻 404） |
| 这家停服但保留密钥 | 后台把商户 `enabled=false`（删除要过"账号→客户端→商户"三道门） |
| **重置了 Agent 密钥** | ★ 这是一次**两件事**的操作：云端改完必须同步改客户机 `config.json` 的 `agent.secret` 并重启 Agent。只改一半 = 这家**立刻整片掉线**，而且客户机会每 13 秒报一次 `4001`（2026-09-26 现网就这么把唯一真商户挡住 50 分钟，当时的日志还把它写成了"本机还有别的 Agent"） |
| 云端代码出问题 | `deploy_cloudapi.py --restore /opt/v3cloud/cloudapi.bak-<ts>` |
| 客户机装错家/装错账套 | 让对方改 `config.json` 的 `agent.name`/`sql.database` 后重启 Agent；**不要**在云端改密钥碰运气 |
| 客户机 exe 要退回 | `deploy-gui.py --list` 看历史包，`--rollback <stamp>`（只作用于开发机发布目录；客户机没有历史包，只能重装） |

## 三条别再踩

1. **绝不跑** `deploy_cloud.py` / `deploy_cloud_update.py`（会重新生成 ADMIN_PWD/AGENT_SECRET/CLIENT_TOKEN
   并重写 `cloud_config.json`，现网密钥全废）。增量走 `deploy_cloudapi.py`。
2. **`all_stores=true` 只能给平台运营者**。客户账号一旦勾了它而平台上又有多家，裸 IP `/`
   会因"该进哪家不明确"回 `400 MERCHANT_REQUIRED`（开第二家时真实撞过），客户老书签当场进不去。
3. 跑 `verify-db-switch-packaged.py` 前必须先停生产 daemon（它起同一个 exe，`kick_duplicates`
   会顶掉生产实例 ⇒ 客户侧整片 502）。那道门现在自己查、查到就 exit 3 拒跑。
   同机隔离语义要验，用**新**的 `verify-multi-agent-host.py`，它不连云端。
