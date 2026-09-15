# thchaos_backend

THChaos 的游戏端与 AstrBot/QQ群之间的实时投票中继。游戏端是唯一权威端；本服务不开奖、不执行 Chaos，只做 WSS 鉴权、房间路由、初步幂等、快照同步和审计。

```
游戏端 TH06NC (Windows) ──wss://域名/ws/game──┐
                                              ├──▶ Caddy :80/:443 ──▶ backend :8765
AstrBot 插件 (QQ 群) ────wss://域名/ws/bot────┘     (自动签发 TLS)     (鉴权/路由/幂等/审计)

QQ 群 ◀── OneBot 反向 WS ── NapCat ◀── AstrBot
```

游戏端产生 `round_id`、选项与票数并执行事件；后端只转发、校验和留痕，不改动任何数值。详细协议见 [docs/protocol-v1.md](docs/protocol-v1.md)。

## 当前状态

- [x] protocol v1 信封、载荷、方向约束和审计错误码
- [x] FastAPI `/ws/game`、`/ws/bot` 双向 WebSocket 中继
- [x] 游戏权威状态、投票快照、开奖和执行结果转发
- [x] SQLite 审计与断线/重连快照
- [x] 模拟游戏端和模拟 Bot
- [x] AstrBot 插件
- [x] thchaos C++ WinHTTP 客户端
- [x] Docker/TLS 生产部署（Caddy 自动签发证书）

## 目录结构

| 路径 | 说明 |
|---|---|
| `src/thchaos_backend/protocol/` | 信封、载荷、错误码等协议模型；不含任何 IO |
| `src/thchaos_backend/server/` | `app`（FastAPI 入口）、`hub`（连接与房间协调）、`config`、`storage`（SQLite 审计） |
| `integrations/astrbot_plugin_thchaos/` | AstrBot 插件，复制到 AstrBot 的 `data/plugins/` 使用 |
| `tools/` | `sim_game.py`、`sim_bot.py` 本机模拟端 |
| `deploy/Caddyfile` | 生产反代配置，域名由 `.env` 注入 |
| `docs/protocol-v1.md` | 协议规范 |

## 协议与限制

| 约束 | 值 | 拒绝方式 |
|---|---|---|
| 首帧 | 必须是 `hello`，且 `seq=1` | `protocol.not_authenticated`（致命） |
| 序号 | 每条连接独立单调 +1；出站序号由服务器独立维护 | `protocol.seq_regression` / `protocol.seq_gap` |
| 单帧大小 | 16 KiB | `protocol.message_too_large` |
| 未知字段 | 一律拒绝 | `protocol.unknown_field` |
| 房间绑定 | `hello.token` 映射出的房间必须等于 `hello.room_id` | `protocol.origin_mismatch`（致命） |
| 游戏端数量 | 每个房间同时只允许 1 个游戏端 | `auth.game_already_connected`（致命） |
| Bot 数量 | 每个房间最多 8 条 Bot 连接（代码常量） | `server.overloaded` |
| 投票频率 | 每条 Bot 连接默认每秒 30 条 `vote.cast` | `protocol.rate_limited`（可重试，不断连接） |

Bot 只能发送 `vote.cast` 和 `heartbeat.ping`，游戏端消息只能由游戏端发送，方向不符即 `protocol.direction_not_allowed`。只有致命错误（鉴权失败、序号回退、帧超限等）会关闭连接，投票被拒/被限流属于单条消息失败，长连接保留。

游戏端断线时房间标记离线并广播 `game.offline`；游戏端重连后必须发送 `game.sync` 全量快照，服务器不会重放断线期间的旧投票。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `THCHAOS_HOST` | `127.0.0.1`（镜像内默认 `0.0.0.0`） | 监听地址 |
| `THCHAOS_PORT` | `8765` | 监听端口 |
| `THCHAOS_DATABASE` | `data/thchaos.sqlite3`（镜像内 `/var/lib/thchaos/thchaos.sqlite3`） | 审计库路径 |
| `THCHAOS_GAME_TOKENS` | 空 | JSON 对象 `{"<游戏Token>":"<room_id>"}` |
| `THCHAOS_BOT_TOKENS` | 空 | JSON 对象 `{"<BotToken>":"<room_id>"}` |
| `THCHAOS_ALLOW_DEV_TOKENS` | `0` | 设为 `1` 才启用内置开发 Token（仅本地模拟） |
| `THCHAOS_ADMIN_TOKEN` | 空 | 为空则不提供 `/rooms/{room_id}/state` 排障接口 |
| `THCHAOS_MAX_CASTS_PER_SECOND` | `30` | 每条 Bot 连接每秒 `vote.cast` 上限 |

Token 长度 1..256 字符，同一房间可以配置多个 Token。生产环境两组 Token 必须不同，且绝不能把游戏 Token 交给 Bot 端。

## 本地开发

Linux / macOS：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
export THCHAOS_ALLOW_DEV_TOKENS=1
export THCHAOS_DATABASE=data/thchaos.sqlite3
.venv/bin/python -m thchaos_backend.server
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
$env:THCHAOS_ALLOW_DEV_TOKENS = "1"
$env:THCHAOS_DATABASE = "data/thchaos.sqlite3"
.\.venv\Scripts\python -m thchaos_backend.server
```

开发 Token（只用于本地模拟）：游戏 `dev-game-token`、Bot `dev-bot-token`，房间均为 `main`。

## Linux 生产部署（Docker + Caddy）

整套栈只有两个容器：`backend`（FastAPI，只在 compose 内网暴露 8765）和 `caddy`（对外 80/443，反代 WebSocket 并自动签发证书）。**域名只写在 `.env` 里，`deploy/Caddyfile` 不需要修改。**

### 0. 前置条件

- Ubuntu 22.04 / 24.04（x86_64 或 arm64），root 或 sudo 权限
- 一个已解析到本机公网 IP 的域名（A 记录）。**如果同时配了 AAAA 记录，服务器必须真的能走 IPv6**，否则 ACME 验证会失败
- 80 和 443 入站可达：ACME 签发走 80，`wss://` 走 443

### 1. 安装 Docker Engine 与 Compose 插件

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"     # 之后重新登录（或 newgrp docker）使组生效
docker compose version              # 需要 v2，输出形如 Docker Compose version v2.x
```

### 2. 获取代码

```bash
sudo mkdir -p /opt && cd /opt
sudo git clone <仓库地址> thchaos_backend
cd thchaos_backend
```

从本机上传时排除本地产物（Windows 的 `.venv` 无法在 Linux 上使用，本地审计库也不该上服务器）：

```bash
rsync -av --exclude .venv --exclude .pytest_cache --exclude '__pycache__' --exclude '*.sqlite3*' \
    ./thchaos_backend/ user@server:/opt/thchaos_backend/
```

### 3. 生成 Token

```bash
openssl rand -hex 32    # 跑三次：游戏 Token、Bot Token、管理 Token
```

用十六进制可以避开 `.env` 里的引号和 `$` 插值问题。

### 4. 写 `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

```dotenv
THCHAOS_DOMAIN=vote.example.com
THCHAOS_HOST=0.0.0.0
THCHAOS_PORT=8765
THCHAOS_ALLOW_DEV_TOKENS=0
THCHAOS_GAME_TOKENS={"2f1c...游戏Token...":"main"}
THCHAOS_BOT_TOKENS={"9a77...BotToken...":"main"}
THCHAOS_ADMIN_TOKEN=4c02...管理Token...
```

要点：

- 两个 JSON 对象必须写成**单行**，值不加引号；`THCHAOS_GAME_TOKENS` 和 `THCHAOS_BOT_TOKENS` 的键是 Token、值是 `room_id`
- 两边客户端的 `room_id` 都必须等于 Token 映射出的房间，否则连接会被判 `origin_mismatch` 并断开
- `THCHAOS_ADMIN_TOKEN` 只在排障时保留，不需要就删掉这一行（接口自动关闭）
- `.env` 已在 `.gitignore` 中，不会入库

### 5. 放行端口

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

云服务器还要在安全组里放行同样的端口。**不要放行 8765**：它只在 compose 内网使用。

### 6. 启动

```bash
cd /opt/thchaos_backend
docker compose up -d --build
docker compose ps                    # backend 应为 healthy，caddy 为 running
docker compose logs -f backend
```

首次启动 Caddy 会在几十秒内完成证书签发，`depends_on: service_healthy` 保证 backend 健康后 Caddy 才启动。

### 7. 验证

```bash
curl -s https://vote.example.com/healthz          # {"ok":true,...}
docker compose logs caddy | grep -i certificate   # 看到 certificate obtained 即签发成功
```

WebSocket 冒烟测试（在服务器上跑，可验证 DNS + TLS + 中继整条链路）：

```bash
sudo apt install -y python3-venv python3-pip
python3 -m venv /opt/thchaos-tools-venv                  # 故意放在仓库外，避免被 Docker 构建上下文带上
/opt/thchaos-tools-venv/bin/pip install -e /opt/thchaos_backend
cd /opt/thchaos_backend
/opt/thchaos-tools-venv/bin/python tools/sim_game.py \
    --url wss://vote.example.com/ws/game --token '<游戏Token>'
```

打印出「投票已开放，等待 Bot 投票」说明鉴权和 TLS 都通了（没有 Bot 在线时它会等 20 秒后超时退出，属正常）。再开一个终端运行 `tools/sim_bot.py --url wss://vote.example.com/ws/bot --token '<BotToken>'`，看到 `game.sync`、`vote.opened` 广播即 Bot 侧链路正常。

注意：模拟游戏端和真实游戏端会争抢同一个房间，真实游戏端已在线时冒烟测试会收到 `auth.game_already_connected`。请在正式接入游戏端之前做这一步。

### 8. 接入客户端

游戏端：编辑 `th06nc_vote.json`（具体路径显示在游戏投票设置界面底部的「配置文件」一行），关键字段：

```json
{
  "remote_enabled": true,
  "remote_url": "wss://vote.example.com/ws/game",
  "remote_token": "<游戏Token>",
  "remote_room_id": "main",
  "remote_client_id": "th06nc-game"
}
```

AstrBot：把 `integrations/astrbot_plugin_thchaos/` 整个目录复制到 AstrBot 的 `data/plugins/`，然后在配置里填：

| 配置项 | 值 |
|---|---|
| `backend_url` | `wss://vote.example.com/ws/bot` |
| `token` | Bot Token（**不能**用游戏 Token） |
| `room_id` | `main`，与 Token 映射的房间一致 |
| `group_ids` | 允许投票的 QQ 群号白名单，其他群一律忽略 |
| `voter_hmac_secret` | 另一串随机值，用于生成不可逆的 `voter_id` |
| `snapshot_interval_seconds` | 群内票况合并播报间隔，默认 2 秒（内部转发仍是实时的） |

NapCat 不需要连接本后端：按 AstrBot 的 OneBot 适配器配置 NapCat 作为客户端连接 AstrBot（建议反向 WS），后端只会看到 Bot Token 和 HMAC 伪名，不接触 QQ 原始账号。

### 9. 日常运维

日志：

```bash
docker compose logs -f backend
docker compose logs -f caddy
docker compose logs --since 30m backend
```

更新：

```bash
cd /opt/thchaos_backend
git pull
docker compose up -d --build
```

备份审计库（`VACUUM INTO` 生成的是一致性快照，WAL 模式下也安全）：

```bash
docker compose exec -T backend python - <<'PY'
import os, sqlite3
dst = "/tmp/thchaos-backup.sqlite3"
if os.path.exists(dst):
    os.remove(dst)
sqlite3.connect("/var/lib/thchaos/thchaos.sqlite3").execute(f"VACUUM INTO '{dst}'")
print("snapshot written")
PY

docker compose cp backend:/tmp/thchaos-backup.sqlite3 ./backup-$(date +%F).sqlite3
```

恢复（先停后端，正常停止会 checkpoint 并清理 WAL）：

```bash
docker compose stop backend
docker compose cp ./backup-2026-09-15.sqlite3 backend:/var/lib/thchaos/thchaos.sqlite3
docker compose start backend
```

停止与清理：

```bash
docker compose down              # 停止并删除容器，保留审计库与证书
docker compose down -v           # 危险：同时删除审计库和 Caddy 证书，证书会重新签发
```

服务器重启后容器会自动拉起（`restart: unless-stopped`）。

## 故障排查

**Caddy 拿不到证书。** 依次检查：`dig +short vote.example.com` 是否指向本机公网 IP；80 端口是否被别的服务占用（`sudo ss -lntp | grep :80`）；云安全组是否放行 80/443；是否配置了不可用的 AAAA 记录；域名是否挂在 CDN 橙色云后面（先关掉代理再签发）。

**客户端连不上。** `docker compose logs backend` 看不到任何 `hello` 就是链路没到服务器（DNS/防火墙）；看到了 `auth.invalid_token` 说明 Token 抄错了；看到 `protocol.origin_mismatch` 说明客户端 `room_id` 与 Token 映射的房间不一致。确认客户端用的是 `wss://` 而不是 `ws://`。

**第二个游戏端连不上。** 一个房间同时只允许一个游戏端，先确认旧的游戏进程已经退出。

**投票被拒但连接没断。** 这是设计如此，逐条对照 `error.payload.code`：`round.not_open`（当前没有开放投票）、`round.stale`（轮次已过期）、`round.duplicate_vote`（同一用户本轮已投过或消息重复）、`round.game_offline`（游戏端未连接）、`protocol.rate_limited`（超过每秒 30 条，可重试）。

**`/rooms/{room_id}/state` 返回 404。** 没设置 `THCHAOS_ADMIN_TOKEN` 时该接口不存在（有意为之）；设置了则要求请求头 `X-Admin-Token` 完全匹配。

**在服务器上跑 `pytest` 失败，报 `did not receive a valid HTTP response`。** 服务器若设置了 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量，websockets 15 会把连本地 127.0.0.1 的测试流量也送进代理。用 `env -u HTTP_PROXY -u HTTPS_PROXY pytest -q` 运行即可，生产链路（Caddy → backend）不受影响。

**游戏机连不上 `wss://`。** 游戏端使用 WinHTTP 默认代理设置，如果游戏机开着系统代理或加速器，需要把域名加入代理例外或关闭代理。

## 安全清单

- 生产 `THCHAOS_ALLOW_DEV_TOKENS=0`（镜像已默认），并使用随机长 Token
- 游戏 Token 与 Bot Token 分离，轮流换只需改 `.env` 后 `docker compose up -d`
- 不给 `backend` 服务添加 `ports` 映射，8765 永远只在 compose 内网
- `.env` 权限 600；备份出的审计库同样要限制访问（库内含群号和投票伪名）
- 管理接口只在排障期间启用，用完即删 `THCHAOS_ADMIN_TOKEN`

## 附录 A：不用 Docker 的裸机部署

沿用 systemd 托管进程，但 **TLS 需要自己解决**（另装 Caddy/nginx 反代，或让进程只监听 127.0.0.1）。没有把握时请用上面的 Docker 方案。

```bash
sudo apt install -y python3.12-venv
sudo useradd --system --home /var/lib/thchaos thchaos
sudo install -d -o thchaos -g thchaos /var/lib/thchaos
sudo python3.12 -m venv /opt/thchaos_backend/.venv          # 用 root 创建，服务账号只需可读可执行
sudo /opt/thchaos_backend/.venv/bin/pip install /opt/thchaos_backend
```

裸机部署时 `.env` 里还要加一行，否则审计库会落在源码目录里：

```dotenv
THCHAOS_DATABASE=/var/lib/thchaos/thchaos.sqlite3
```

`/etc/systemd/system/thchaos-backend.service`：

```ini
[Unit]
Description=THChaos vote relay
After=network-online.target
Wants=network-online.target

[Service]
User=thchaos
WorkingDirectory=/opt/thchaos_backend
EnvironmentFile=/opt/thchaos_backend/.env
ExecStart=/opt/thchaos_backend/.venv/bin/python -m thchaos_backend.server
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now thchaos-backend
```

## 附录 B：本地模拟端

`tools/sim_game.py` 与 `tools/sim_bot.py` 不连接真实游戏或 QQ，用于在没有游戏机时验证后端：

```bash
.venv/bin/python tools/sim_game.py --url ws://127.0.0.1:8765/ws/game --token dev-game-token
.venv/bin/python tools/sim_bot.py  --url ws://127.0.0.1:8765/ws/bot  --token dev-bot-token
```

`sim_bot` 从标准输入读取 `1`/`2`/`3` 并提交投票，同时打印收到的全部广播。模拟端的 `voter_id` 固定，因此一轮里只能成功投一票。

## 测试

```bash
.venv/bin/pytest -q        # Linux
.\.venv\Scripts\pytest.exe -q   # Windows
```

## 许可证

Proprietary，见 `pyproject.toml`。
