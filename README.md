# pi-telegram-tui-bridge

用 Telegram 跟**正在开着的 pi TUI 会话**聊天。消息投进 TUI（等同你在窗口里敲回车），回答从会话 jsonl 抽出来发回 Telegram。

没有无头 session，也不额外调模型：桥只做「Telegram 长轮询 ↔ 文件握手 ↔ `pi.sendUserMessage()`」，token 消耗跟你直接在键盘上说话**完全一样**。

## 组成

| 文件 | 作用 |
|---|---|
| `pi-telegram-tui-bridge.ts` | pi 扩展。监听 `%TEMP%\pi-telegram-tui-bridge\<sid>.req`，调 `pi.sendUserMessage()` 投递；`agent_settled` 时写 `<sid>.settled`；在 `<sid>.alive` / `current.json` 里登记在线会话 |
| `package.json` | pi 包声明：`pi.extensions` 指向上面的 `.ts`。装了它就不需要往 `~\.pi\agent\extensions\` 复制副本 |
| `telegram-tui-bridge.py` | 后台进程。Telegram 长轮询 → 选目标 TUI 会话 → 写 `.req` → 等 `.settled` → 从 jsonl 抽本轮 assistant 文本 → 分片发回 |
| `ensure-bridge.ps1` | 看门狗：桥不在就拉起（可挂计划任务，5 分钟一次） |
| `install.ps1` | 一键：`pi install <本目录>` 登记包 + 检查 `notify.json` + 重启桥 |
| `notify.example.json` | 配置样板：token / proxy / chatId |

运行时产物（日志、offset、pid）写在 `~\.pi\agent\`，不进仓库：

- `telegram-tui-bridge.log` / `-watchdog.log`
- `telegram-tui-bridge-state.json`（只存 Telegram `offset`）
- `telegram-tui-bridge.pid`
- `notify.json`（含 bot token，**别提交**）

## 安装（新机器）

1. 依赖：Windows、Python 3（`pythonw.exe` 在 PATH 里即可，不要求装在固定路径）、pi、一个 Telegram bot（`@BotFather`）。
2. `git clone` 本仓库，随便放哪都行（脚本全部相对自身路径）。
3. 建配置：把 `notify.example.json` 复制成 `%USERPROFILE%\.pi\agent\notify.json`，填三项——
   - `token`：BotFather 给的
   - `chatId`：你自己的 telegram id
   - `proxy`：需要代理才填（如 `http://127.0.0.1:7897`），直连留 `""`
   缺这个文件 `install.ps1` 会直接报错退出，因为桥在 pythonw 里崩了你看不见。
4. 登记成 pi 包：在仓库目录里跑 `pi install .`。
   - 扩展由 pi 直接从这个目录加载（本地包不复制），改完 `git pull` 或改文件后只需 `/reload`。
   - 不想装后台依赖、只要扩展的话，`pi install git:github.com/LittleGuo/pi-telegram` 也行。
   - 已装过旧版（`~\.pi\agent\extensions\pi-telegram-tui-bridge.ts` 有副本）记得把副本删掉，否则扩展会被加载两次、同一条消息投递两遍。
5. 跑 `pwsh -File install.ps1`（检查配置 + 重启桥；它会顺手再跑一次幂等的 `pi install`）。
6. 在 pi TUI 里执行 `/reload`（不用关会话）。
7. 验证：给 bot 发一条消息，日志应出现 `目标会话 sid=…` → `投递方式 mode=…` → `已回复`。

可选：把 `ensure-bridge.ps1` 挂成计划任务（5 分钟一次），桥挂了会自动拉起。

### 更新

- 只改了扩展：`git pull` → pi 里 `/reload`。
- 改了桥（`.py`）：`git pull` → `pwsh -File install.ps1`（或直接跑 `ensure-bridge.ps1` 前先杀掉旧进程）。
- `pi list` 能看到它，`pi remove <路径>` 只注销扩展，不停后台桥。

## 换机器 / 两台机器

**同一个 bot token 只能有一个进程在 `getUpdates`**，两边都开着会互抢（HTTP 409），消息随机落到旧机器上。所以换机器时顺序是：

1. 旧机器先停桥：
   ```powershell
   Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*telegram-tui-bridge.py*' } |
       ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```
   如果挂了计划任务，一并 `Disable-ScheduledTask`。
2. 新机器按上面七步装。
3. `notify.json` 不在仓库里（含 token），要么从旧机器复制，要么手填。

状态不进仓库：日志、`offset`、pid 都在新机器的 `~\.pi\agent\` 下重新生成。`offset` 不带走也没关系，Telegram 会重放最多 24 小时内未确认的消息。

## 使用限制（脚本里是硬编码）

- pi 窗口必须开着；关了会回「找不到正在开着的 pi TUI 会话」。
- 一轮最多等 15 分钟（`TURN_TIMEOUT`）；超时把已有文本发回并标注超时。
- 输入超过 4000 字截断；回答按 3900 字分条发送。
- 只认文字消息。
- 一轮只取最后的 assistant 文本；只跑工具没写字会回「没有可发送的助手文本」。
- 不能中途插话 / 打断，也看不到工具调用和 diff。
- 同时开多个 pi 窗口时投给**最近有活动**的那个（`.alive` 在每轮结束时刷新）。

## 握手协议（`%TEMP%\pi-telegram-tui-bridge\`）

| 文件 | 谁写 | 含义 |
|---|---|---|
| `<sid>.req` | 桥 | 原子写入的投递请求 `{sid,text,mode,ts}`，`mode` = `auto` / `steer` / `followUp` |
| `<sid>.alive` | 扩展 | 该会话是 TUI 且扩展已加载 |
| `<sid>.settled` | 扩展 | 一轮结束（`aborted` 标记是否被打断） |
| `current.json` | 扩展 | 最近活跃的 TUI 会话元数据 |

## 已知坑

- **会话 jsonl 是收到第一条用户消息才创建的**。新会话刚开、你还没敲字时文件不存在——早期版本会把这种情况误判成「扩展没加载」并完全不投递，现已修（`iter_jsonl_from` 容忍文件缺失，投递前不再检查 `isfile`）。
- **旧的无头桥 `telegram-bridge.py` 必须停掉**：同一个 bot 两个 `getUpdates` 会 409 冲突，且消息会进只读的 `session-id=telegram-bridge` 会话。
- 扩展/桥版本是**两份文件**（仓库里的源 + `~\.pi\agent\extensions\` 里的副本）。改了 `.ts` 要跑一次 `install.ps1`，否则 `~/.pi/agent/extensions` 里那份旧代码继续生效。
- pi 异常退出不会跑 `clearPresence`，`.alive` 会残留；此时现象是「扩展没有取走请求文件」而不是「找不到会话」。
