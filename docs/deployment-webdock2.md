# WebDock2 生产环境部署与运维说明 (Runbook)

本文档记录 `ai-quota-monitor` 在宿主机 `webdock2`（Windows 11 WSL2 Ubuntu-24.04）上的生产拓扑、配置方式、数据持久化与断电自愈机制。用于在新设备上快速迁移、标准化部署及避免重复排障。

---

## 1. 架构拓扑与链路

```
[ChatGPT / Claude / X]
         ▲
         │ (出境代理: host.docker.internal:7897 / mihomo)
┌────────┴──────────────────────────────────────────────────────┐
│ webdock2 (WSL2 Ubuntu-24.04-WebDock)                         │
│                                                               │
│  Docker 容器: quota-monitor (ghcr.io/huozao/ai-quota-monitor) │
│   ├── DISPLAY=:101 (Xvfb 1366x768, x11vnc :5902, noVNC :6082) │
│   ├── Chrome #1 (port 9224): 主账号 codex (ishell168) + claude│
│   ├── Chrome #2 (port 9225): 辅账号 codex_2 (www.nada.cn)     │
│   ├── FastAPI 服务 (port 8001 -> 宿主机 18002)                │
│   └── SQLite & 截图持久化卷                                   │
└───────────────────────────────────────────────────────────────┘
         ▲                                     │ (HTTP 投递)
         │ (SSH 反向隧道: 16094 / 16093)        ▼
┌────────┴──────────────────────────────────────────────────────┐
│ txecs (公网节点)                                              │
│   ├── Nginx 反代: /console/quota/api/ -> webdock2:18002       │
│   ├── 控制台静态页: https://hydwang.xyz/console/quota/        │
│   └── 通知中枢: business-cn-backend-api-1 (Feishu Card 2.0)   │
└───────────────────────────────────────────────────────────────┘
```

---

## 2. 宿主机数据卷与持久化规划

生产宿主机将关键数据映射至 `/var/lib/webdock/`（或 `/var/lib/ai-quota-monitor/`）：

| 宿主机路径 | 容器内挂载路径 | 作用与生命周期 |
|---|---|---|
| `/var/lib/webdock/quota_browser_data` | `/app/quota_browser_data` | **主账号 Chrome Profile**：端口 9224 浏览器数据，存放主账号 Cookie 与登录态。**严禁清理！** |
| `/var/lib/webdock/quota_browser_data/account_9225` | `/app/quota_browser_data/account_9225` | **辅账号 Chrome Profile**：端口 9225 独立浏览器数据，隔离辅账号 Cookie。**严禁清理！** |
| `/var/lib/webdock/quota_data` | `/app/quota_data` | 运行状态数据，包含 SQLite 库（`quota.sqlite3`）、现场截图目录（`screenshots/`）。 |
| `/var/lib/webdock/quota_data/ATTACH_ENABLED` | `/app/quota_data/ATTACH_ENABLED` | **采集使能门控文件**：人工在 noVNC 完成登录后 `touch` 创建。缺失时容器只跑浏览器不触发 Playwright attach。 |
| `/var/log/webdock/quota_logs` | `/app/quota_logs` | Xvfb、x11vnc、noVNC 及多 Chrome 实例标准输出日志。 |

> [!IMPORTANT]
> 浏览器的 Cookie 和 LocalStorage 均保存在宿主机卷中。设备遭遇断电或容器重启时，浏览器 Profile 完好无损，开机后无需重新登录。

---

## 3. 环境变量与配置参数 (`compose.yml`)

在 webdock2 的 `/opt/webdock/deploy/laptop/.env` 中配置：

```bash
# 镜像标签（生产采用 GitHub Actions 构建的不可变 SHA 镜像）
QUOTA_IMAGE=ghcr.io/huozao/ai-quota-monitor:sha-<commit_sha>

# 基础显示与安全
DISPLAY_WIDTH=1366
DISPLAY_HEIGHT=768
VNC_PASSWORD=<自定义强密码>

# 代理设置（指向宿主机的本地代理网关，访问 ChatGPT/Claude 必需）
CHROME_PROXY_SERVER=http://host.docker.internal:7897

# 多账号声明（格式：id:端口:用户别名）
# 端口 9224 为首实例，9225 为次实例（各占一半屏幕 680x768 并列排布）
QUOTA_CODEX_ACCOUNTS="codex:9224:ishell168,codex_2:9225:www.nada.cn"

# 轮询策略、页面休眠与保留周期
QUOTA_POLL_MINUTES_MIN=20
QUOTA_POLL_MINUTES_MAX=30
QUOTA_PAGE_SETTLE_SECONDS=5
QUOTA_PAGE_HIBERNATE=true
QUOTA_RETENTION_DAYS=7
QUOTA_REPORT_TIMES="08:00,13:00,20:00"

# 外部展示与通知中枢
QUOTA_PUBLIC_API_PREFIX=/console/quota/api
QUOTA_PUBLIC_LINK=https://hydwang.xyz/console/quota/
NOTIFY_ENDPOINT=http://host.docker.internal:18020/v1/internal/notify/send
NOTIFY_SOURCE_TOKEN=<通知来源Token>
```

Docker Compose 服务定义关键属性（统一工程门控）：
```yaml
services:
  quota-monitor:
    image: ${QUOTA_IMAGE:-ghcr.io/huozao/ai-quota-monitor:latest}
    container_name: quota-monitor
    restart: unless-stopped
    profiles: ["quota"]
    security_opt:
      - seccomp=unconfined
    shm_size: "2gb"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "127.0.0.1:18002:8001"
      - "127.0.0.1:6082:6082"
    volumes:
      - /var/lib/webdock/quota_browser_data:/app/quota_browser_data
      - /var/lib/webdock/quota_data:/app/quota_data
      - /var/log/webdock/quota_logs:/app/quota_logs
```

---

## 4. 断电与冷重启自愈设计 (Power Loss & Auto-Recovery)

当宿主机因停电整机断电并在来电后开机时，系统设计具备如下自愈链条：

1. **Docker 守护进程与容器自动拉起**：
   - 容器设置了 `restart: unless-stopped`，Docker 随 WSL 启动后自动拉起 `quota-monitor`。
2. **Xvfb 残留锁自动探测与安全清理**：
   - 非正常断电可能在容器层残留 `/tmp/.X101-lock` 和 `/tmp/.X11-unix/X101`。
   - `docker/quota-entrypoint.sh` 在启动前探测锁持有的 PID，**仅当该 PID 不存在或处于 Zombie 状态时才执行安全清理**，杜绝了误删正在运行的 X 服务的风险，同时避免因陈旧锁导致 Xvfb 起不来而假死。
3. **多 Chrome 实例分屏拉起**：
   - 入口脚本根据 `QUOTA_CODEX_ACCOUNTS` 自动计算分屏坐标（`0,0 680x768` 与 `680,0 680x768`），分别挂载独立 profile 并监听 CDP 端口（`9224` 与 `9225`）。
4. **健康检查多端口校验**：
   - `/healthz` 遍历检查所有声明的 CDP 端口（`browser_cdp_ports: {"9224": true, "9225": true}`），任一浏览器未就绪则返回 `503`，确保监控不会误报健康状态。
5. **门控文件与采集恢复**：
   - 数据卷中的 `ATTACH_ENABLED` 持久存在，采集循环 `_loop` 检测到后自动开始定时轮询。
6. **轻量页面休眠与内存释放**：
   - 每次采集完成后标签页切至 `about:blank#quota-target=<id>` 并主动触发 V8/Native GC，使长期空闲时的 Chrome 渲染进程物理内存（RSS）由 ~200MB 降至 ~20MB。
   - 遇到 `auth_required` / `blocked` 时自动保持原页面不休眠，方便用户在 noVNC 桌面中直观查看登录状态并完成认证。

---

## 5. 新设备部署全流程 (New Setup Checklist)

在新机器上执行部署的步骤：

1. **准备宿主机目录**：
   ```bash
   sudo mkdir -p /var/lib/webdock/quota_browser_data /var/lib/webdock/quota_data /var/log/webdock/quota_logs
   sudo chown -R 1000:1000 /var/lib/webdock/quota_browser_data /var/lib/webdock/quota_data /var/log/webdock/quota_logs
   ```
2. **写入配置文件**：
   按照第 3 节配置 `.env` 与 `compose.yml`。
3. **拉取镜像并启动**：
   ```bash
   docker compose --profile quota up -d
   ```
4. **人工完成初次登录**：
   - 浏览器打开 `http://<宿主机IP>:6082` 进入 noVNC 桌面。
   - 左侧窗口登录主账号（`ishell168`），并打开 Claude；右侧窗口登录辅账号（`www.nada.cn`）。
   - 确认各账号均能正常看到额度页面。
5. **激活采集门控**：
   ```bash
   touch /var/lib/webdock/quota_data/ATTACH_ENABLED
   ```
6. **就绪验证**：
   ```bash
   # 验证健康检查及两个 CDP 端口就绪
   curl -s http://127.0.0.1:18002/healthz | jq .
   # 查看采集记录入库
   docker exec quota-monitor python -c "import sqlite3; [print(r) for r in sqlite3.connect('/app/quota_data/quota.sqlite3').execute('SELECT id,provider,status,captured_at FROM captures ORDER BY id DESC LIMIT 4')]"
   ```

---

## 6. 排障取证与规范 (Runbook)

- **判断是否前端 SPA 改版**：
  不要凭空猜测，直接读取当前数据库内保存的原始渲染文字：
  ```bash
  docker exec quota-monitor python -c "import sqlite3; print(sqlite3.connect('/app/quota_data/quota.sqlite3').execute('SELECT text FROM captures WHERE provider=\"codex\" ORDER BY id DESC LIMIT 1').fetchone()[0])"
  ```
- **检查现场截图**：
  访问 `http://127.0.0.1:18002/v1/quota/captures/<id>/screenshot`，查看页面现场真实画面。
- **状态流转说明**：
  - `healthy`：额度与关键小节完整解析。
  - `partial`：解析出部分辅助信息（如手动重置次数），但核心额度百分比缺失（多为页面文案改版）。
  - `loading`：页面正在加载中（网络延迟），下一轮轮询会自动重试。
  - `auth_required`：页面重定向至登录墙，需人工在 noVNC 重新认证。
  - `blocked`：访问受限或触发人机验证（如 Cloudflare Turnstile 质询盾），严禁休眠并保持现场供人工直接点击通过。
  - `schema_changed`：未识别到任何有效字段，页面结构发生彻底变更（同样跳过休眠保留现场）。
  - **休眠门禁原则**：仅在 `healthy` / `partial` 成功解析时进入 `about:blank` 轻量休眠；遇到上述任何非健康状态时严格保留真实页面，避免休眠卡片掩盖人机验证或异常现场。

---

## 7. 典型案例与设计意图 (Case Studies)

### 案例 1：2026-10 ChatGPT 额度页改版与小节锚定
- **现象**：国庆停电后重启，日报中 Codex 账号显示「5h: 暂无数据，周额度: 暂无数据，partial」，但现场截图实际显示完整额度。
- **根因**：OpenAI 调整了前端 DOM 文本渲染：
  - 5h 小节由旧版 `5-hour usage limit` 变为 `5-hour limit\nResets in <time>\n<num>% left`。
  - 周限额小节由旧版 `Weekly limit` 变为同构的 `Weekly limit\nResets in <time>\n<num>% left`。
  - 原正则按 `remaining` 匹配导致漏检 `left`，且全页首条 `Resets in` 会因小节未锚定而错位。
- **解法**：
  1. `quota_monitor/core.py` 中的 `CODEX_SECTIONS` 支持元组多候选标记：`("five_hour", ("5-hour limit", "5-hour usage limit"))`。
  2. `section_resets` 动态搜索首个匹配标记并确定各小节文本块区间。
  3. `quota_monitor/app.py` 优先从小节块中按 `(?:remaining|left)` 提取剩余百分比，并支持 Credits 数值前置/后置。

### 案例 2：观察位（@thsottiaux）动态双语呈现
- **设计意图**：针对 X 观察位的发布动态，英文保留为主要展示文本，中文翻译作为辅助说明。
- **排版约定**：
  - 飞书卡片：正文为主，中文设为浅灰细字 `<font color='grey'>翻译：{text_zh}</font>`。
  - 网页控制台（`https://hydwang.xyz/console/quota/`）：主段落展示英文原文，下方附 `<p class="post-zh" style="color:var(--muted);font-size:.82rem;margin:2px 0 6px;line-height:1.45;">翻译：${text_zh}</p>`。
- **翻译通道**：通过 `translate.googleapis.com` 请求（受 `CHROME_PROXY_SERVER` 路由代理保护），采用内存 LRU 缓存（256 条）避免重复调用与网络抖动。

### 案例 3：容器刚重启首轮采集的 SPA 渲染延迟与骨架屏轮询优化
- **实测观察**：容器冷启动后首轮采集可能因 Chrome 9225 尚未完成首屏 JS 水合（DOM innerText 暂时为空或显示 "Loading usage settings…"）返回单次 `partial` / `loading`。
- **2026-10-29 演进**：在 `_capture` 中增加 SPA 异步数据加载与骨架屏轮询等待机制。若遇到 `loading` 状态，以 1.5s 间隔轮询等待最多 15s；若仍未就绪则自动触发单次 `page.reload` 补救重试（类似人工刷新机制），彻底避免首屏加载慢或 React 临时骨架屏被直接落库记录为 loading。无需人工进入 noVNC 手动刷新。

### 案例 4：2026-10 页面轻量休眠、内存暴降 60% 与孤儿标签页治理
- **背景与诉求**：双 Chrome 实例（9224 主账号 3 个标签页 + 9225 辅账号 1 个标签页）长期挂载 React 重型单页应用（ChatGPT / Claude / X），导致渲染进程虚拟 DOM 与 JS 堆内存持续累积，容器内存占用高达 2.22 GiB (19%)。用户希望降低常驻内存。
- **技术选型权衡**：
  - *为何不采用按需杀死 Chrome 进程？* 每次冷启动访问受强反爬保护的 ChatGPT / Claude 时，极易触发 Cloudflare Turnstile 人机验证质询盾导致自动化采集失败；且冷启动会导致 noVNC 人工登录与观察机制断层。
  - *方案 A（轻量休眠）*：浏览器进程保持在线，采集完成后标签页切至 `about:blank#quota-target=<id>` 并触发 CDP V8/Native 垃圾回收（`HeapProfiler.collectGarbage` + `Memory.forciblyPurgeJavaScriptMemory`）。轮询触发时自动唤醒回额度页；发生认证异常（`auth_required` / `blocked`）时强制跳过休眠，保持原登录界面供人工排查。
- **踩坑与实战演进**：
  1. *Chrome 崩溃恢复气泡（Restore pages?）*：非正常停止容器导致 Chrome 记录异常退出标记。启动时若触发 Session Restore 会自动恢复旧页面导致标签页倍增。解法：在 `docker/quota-entrypoint.sh` 启动前用 Python 重置 `Preferences` 里的 `exit_type: "Normal"`，并注入启动参数 `--hide-crash-restore-bubble`。
  2. *孤儿标签页（多余页面）清扫*：旧逻辑仅清理同一 target 的同源重复页，无法清理崩溃恢复产生的未认领页面。解法：在 `quota_monitor/app.py` 中记录 `claimed_pages`，每轮循环结束后将所有未被合法 target 认领的多余页面执行 `page.close()`，严格保证 9224 端口精确 3 个标签页、9225 端口精确 1 个标签页。
- **实测指标（2026-10-10 实测）**：
  - 容器内存由 **2.22 GiB (19%)** 暴跌至 **901 MiB ~ 1.28 GiB**（内存净降幅达 40%~60%，节约超过 1GB 物理内存）；
  - 空闲 CPU 占用由原本的 13% 降至 **0.26% ~ 0.28%**；
  - 活跃 PIDS 由 412 降至 306~347。

### 案例 5：2026-10 部署配置漂移事故与 SSOT（SOPS）权威闭环
- **事故现象**：宿主机更新 webdock 容器后，ai-quota-monitor 突发推文翻译丢失、周趋势图 404、辅账号 codex_2 停采、主账号额度百分比缺失（返回 None）。
- **根因分析**：
  - 宿主机 `/opt/webdock/deploy/laptop/.env` 并非权威正本，而是由 `infra` 仓库 `render.sh webdock2` 从 `secrets/webdock2.enc.env` 解密单向生成。
  - 此前 quota-monitor 新增的多账号（`QUOTA_CODEX_ACCOUNTS`）及休眠配置直接在宿主机 `.env` 热修生效，未回写 `infra` 的 SOPS 加密文件；且镜像 pin 仍停留在 9 月旧版。
  - webdock 正常部署执行 `render.sh` 时，完全忠实地按照 `infra` 主线重新生成 `.env`，导致宿主机手写配置被冲掉、镜像被强行降级回 9 月旧版。
- **解法与铁律**：
  1. 严格贯彻 **Git 为唯一权威源（SSOT）**：所有宿主机环境配置必须通过 `sops set` 写入 `infra/secrets/webdock2.enc.env` 并合入 `main`，严禁在宿主机脱离 Git 长期维护手写配置。
  2. 部署时通过 `render.sh` 统一安全下发，确保任意服务重建都不会造成配置回退。

### 案例 6：Cloudflare 验证盾识别（blocked）、休眠门禁白名单收紧与人机认证协同
- **设计缺陷与现象**：
  - Claude 触发 Cloudflare Turnstile 人机验证盾（`Performing security verification`）时，原正则仅识别 `log in` / `sign in` 为 `auth_required`，缺少拦截关键词匹配，导致将其错误判定为 `schema_changed`。
  - 原休眠门禁采用黑名单豁免（`not in ("auth_required", "blocked")`），漏过了 `schema_changed`，导致页面在截图后立马切至 `about:blank#quota-target=claude` 休眠卡片，掩盖了真实质询盾现场。
  - 用户在 noVNC 打开后台浏览器时只见休眠卡片，不见验证盾，无法手动点击；下轮轮询唤醒时再次撞盾，陷入死循环。
- **解法**：
  1. `_parse` 与 `_parse_posts` 增加 Cloudflare / Turnstile 质询文案匹配，准确返回 `status = "blocked"`。
  2. 休眠门禁全面收紧为**白名单模式**：`if PAGE_HIBERNATE and result.get("status") in ("healthy", "partial")`。凡是 `blocked`、`auth_required`、`schema_changed` 等异常状态，**一律禁止休眠，强行保留页面现场留在屏幕上**，确保人工随时在 noVNC 看到并操作。
  3. **人机认证与探测机制**：额度监控器为 20~30 分钟一次的离散轮询（抓完即断开 CDP），在休眠间歇期浏览器为绝对静止纯净状态，人工可随时在 noVNC 点击通过质询；若需长时间复杂认证，可通过删除 `ATTACH_ENABLED` 门控文件彻底暂停轮询探测。

### 案例 7：Claude 历史会话标题干扰登录态识别与 5h/周额度分段解析
- **事故现象与边界条件**：
  - 人工通过 Cloudflare 盾完成验证后，页面进入正常 Usage 界面且能看到额度，但随后的采集却被误判为「需重新登录」（`auth_required`），且 5 小时和周额度无法解析。
  - **根因一（假未登录）**：Claude 侧边栏历史会话列表中含有如「终端CLI登录连接失败」等标题。原 `_parse` 逻辑无边界匹配 `"登录"`，命中会话标题中的文字，导致已登录且展示额度的页面直接返回空字段和 `auth_required`。
  - **根因二（数值前置与小节未分段）**：Claude 现行 UI 渲染文本格式为 `0% used` / `12% used`（数值前置于 `used`），原正则仅支持后置；且文本中包含 `Current session` 与 `This week` 两处 `used`，原逻辑未按小节分别提取，导致周额度缺失；周限额标记缺少 `this week`，引导词 `Starts with your first message` 未适配导致无法识别 `idle`。
- **解法**：
  1. **登录态上下文门禁**：在判定未登录前，优先识别当前是否已处于额度或设置面板视图（`is_usage_view`：包含 `"your usage"`, `"plan limits"`, `"usage limits"`, `"log out"`, `"sign out"`）。若处于面板视图，会话标题中的词汇绝不触发 `auth_required`；真实的未登录页面改用严格引导短语（`"please log in to"`, `"sign up or log in"`, `"enter your email"`, `"请先登录"` 等）。
  2. **小节边界结构化解析**：`CLAUDE_SECTIONS` 周小节标记候选增加 `"this week"`，右边界增加 `"limit resets"`；分别从 `session_block` 提取 5 小时 `used`（`0%`）与 `remaining`（`100%`），从 `weekly_block` 提取周 `weekly_used_percent`（`12%`）与 `weekly_remaining`（`88%`），重置时间准确锚定。
  3. **数值前置与后置双向正则**：`used_pat` 与 `remaining_pat` 调整为兼顾 `0% used` 与 `used: 0%`，提取时统一取 `match.group(1) or match.group(2)`。
  4. **会话空闲态识别**：`_SESSION_IDLE_PATTERNS` 增加 `"starts with your first message"`，准确识别 `session_state: "idle"`。
  5. **DOM 进度条优先**：在 `_capture` 中若检测到 `[role=meter]` 进度条，进一步互补提取与校准。

### 案例 8：2026-10 网页翻译再次消失（控制台页只在同步分支上部署过）
- **现象（2026-10-25 发现）**：X 观察位帖子在飞书卡片里有中文翻译，网页控制台没有。`/v1/quota/latest` 的 `posts[].text_zh` 正常下发，问题不在本仓。
- **根因**：网页是 infra 的 `roles/server/tencent/files/console-quota.html`，由 infra 的 tencent 角色 `apply.sh` 渲染到边缘主机。案例 2 的翻译渲染（infra `85143c9`）只推到了同步分支，从那里 apply 过，从未合入 infra main；2026-10-17 有人为别的改动从 main apply，页面被覆盖回旧版。本仓镜像 pin 走的是另外几次已合入 main 的提交，所以后端一直是新的。
- **处理**：渲染代码已合入 infra main 并重新 apply（线上哈希与仓库一致，2026-10-25 实测）。infra 侧同时加了三道防线：apply/render 拒绝部署 `origin/main` 之外的提交；版本看板漂移检查对比线上 `console-quota.html` 与 main；工作区每日体检列出远端分支上未进 main 的部署改动。
- **教训**：本仓「推到 GitHub」不等于网页生效——控制台页和镜像 pin 都在 infra，必须进 infra **main** 才算交付。改网页显示时同时核对线上文件，不能只看 API 字段。

### 案例 9：2026-10 Claude 验证盾死锁、Playwright 驱动崩溃与 SIGCHLD 劫持致轮询停摆
- **事故现象（2026-10-29 发现）**：监控从 10-28 18:02 后停止采集，控制台无新数据。Uvicorn 依然存活且 `/healthz` 返回 200，但后台采集主协程彻底死锁。
- **根因分析**：
  1. *诱因*：Claude 触发 Cloudflare Turnstile 验证盾，受 `--renderer-process-limit=2` 资源挤压，其渲染进程陷入 `futex_do_wait` 死锁，CDP 通讯失去响应。
  2. *驱动崩溃*：Playwright `connect_over_cdp` 超时 180s 强行断开，底层 Node.js 驱动因未捕获 `Network.setCacheDisabled` 会话关闭异常而触发 `triggerUncaughtException` 退出。
  3. *致命死锁*：`app.py` 中 `_setup_subreaper` 粗暴安装了全局 `signal.signal(signal.SIGCHLD, ...)`，导致 Node.js 退出状态被提前 `waitpid` 收割，绕过了 Python `asyncio` 的 Child Watcher。Playwright 上下文退出时执行 `await proc.communicate()`，陷入对已被收割进程的永久死锁。
  4. *自愈盲区*：旧版 `/healthz` 仅探测端口 TCP，后台协程已死仍误报 200，容器未被 Docker 重启。
- **解法**：
  1. 去除 `_setup_subreaper` 中的全局 `SIGCHLD` 信号劫持，改在循环间隙通过 `reap_zombies()` 安全清理僵尸进程，恢复 `asyncio` 内部子进程生命周期管理。
  2. 移除 entrypoint 中的 `--renderer-process-limit=2`，保持标准 site 隔离，防止多域名单进程锁死。
  3. 强化 `/healthz`：主动校验后台 `_task` 存活状态及心跳周期（超过 `POLL_MAX + 10m` 未推进判定 stalled），异常时返回 503 触发 Docker 自动拉起自愈。
  4. 为 `connect_over_cdp` 显式设置 30s 超时，杜绝 180s 长时间 hang 阻断循环。

---

## 6. 核心交付准则与部署闭环 (SSOT)

- **GitHub 作为唯一权威源码 (SSOT)**：
  任何逻辑修复、解析器演进或配置调整，在本地通过测试套件充分验证（`pytest`、`compileall`、`git diff --check`）后，**必须提交并推送到 GitHub 源码仓库，严格以 GitHub 源码为准执行**。严禁在生产宿主机上进行任何形式的手工热补丁或文件篡改。
- **标准化容器化交付 (GitOps)**：
  生产环境**最终必须以容器化镜像方式进行部署**。流程严格遵守：
  `本地验证 -> Git Commit & Push -> GitHub Actions 自动构建镜像 (ghcr.io/huozao/ai-quota-monitor:sha-<commit>) -> infra 仓库 SOPS 配置更新镜像 Tag -> 宿主机 render.sh 单向安全下发重启`。杜绝任何配置漂移与部署回退。
