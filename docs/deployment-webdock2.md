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

# 轮询策略与保留周期
QUOTA_POLL_MINUTES_MIN=20
QUOTA_POLL_MINUTES_MAX=30
QUOTA_PAGE_SETTLE_SECONDS=5
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
  - `schema_changed`：未识别到任何有效字段，页面结构发生彻底变更。
