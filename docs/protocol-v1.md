# THChaos protocol v1

## 目标与权威边界

游戏端是投票的唯一权威：它产生 `round_id` 和选项、推进游戏时间、接受/拒绝投票、统计票数、关票、决定结果并执行 Chaos 事件。后端只负责鉴权、房间路由、幂等/限流、快照和审计，不关票、不开奖、不执行事件。

当前游戏实现的语义固定为：平票时三个选项全部执行；无人投票时随机执行一个选项；暂停冻结游戏计时但仍可收票。文档中的旧 UDP 规则不适用于本协议。

游戏端和 AstrBot 插件均主动连接服务器的 WSS：`/ws/game` 与 `/ws/bot`。连接建立后客户端发送 `hello`，`seq` 从 1 开始且每条连接单调递增。服务器向每条连接独立维护自己的出站 `seq`。

## 信封

每帧是 UTF-8 JSON，最大 16 KiB，未知字段拒绝：

```json
{
  "version": 1,
  "type": "vote.opened",
  "message_id": "550e8400-e29b-41d4-a716-446655440000",
  "room_id": "main-room",
  "game_instance_id": "game-20260915-a",
  "seq": 12,
  "sent_at": "2026-09-15T12:00:00.000Z",
  "payload": {}
}
```

`message_id` 是幂等键；`game_instance_id` 与 `round_id` 共同标识一次运行中的轮次，不能只使用 `round_id`。重连后游戏必须发送 `game.sync` 全量快照，服务器不会把断线期间的旧用户投票重放给游戏。

## 消息方向

| 类型 | 游戏端 → 后端 | Bot → 后端 | 后端 → 游戏端 | 后端 → Bot |
|---|---:|---:|---:|---:|
| `hello` | ✓ | ✓ |  |  |
| `game.sync` / `game.state_changed` | ✓ |  |  | ✓ |
| `vote.opened` / `vote.snapshot` / `vote.closed` | ✓ |  |  | ✓ |
| `vote.cast` |  | ✓ | ✓ |  |
| `vote.ack` / `effect.resolved` | ✓ |  |  | ✓ |
| `heartbeat.*` | ✓ | ✓ | ✓ | ✓ |
| `error` |  |  | ✓ | ✓ |

## 轮次时序

1. 游戏发送 `vote.opened`，包含恰好三个按 `choice=1,2,3` 排列的选项。
2. 后端广播给 Bot。Bot 只在活动轮次且群在白名单时接受整条消息为 `1`、`2` 或 `3`。
3. Bot 为用户生成 HMAC 伪名，发送 `vote.cast`。后端验证房间、实例、轮次、在线状态并以 `(game_instance_id, round_id, voter_id)` 做初步幂等检查，然后转发给游戏。
4. 游戏发送 `vote.ack`。只有 `status=accepted` / `counted=true` 才算有效；Bot 不自行增加票数。
5. 游戏按需要发送 `vote.snapshot`，票数以它为准。后端可以把快照合并节流后发送到群里，但不能改动数值。
6. 游戏发送 `vote.closed`，列出一个胜者、三个平票胜者或一个随机胜者。
7. Chaos 邮箱真正处理后，游戏为每个命令发送 `effect.resolved`；Bot 以此做最终同步汇报。

暂停只由游戏端报告，后端不使用服务器墙钟关闭轮次。`remaining_ms` 只能作为展示值。

## 错误与恢复

服务器侧拒绝使用 `protocol.*`、`auth.*`、`round.*` 错误码；它们表示消息不能继续转发。游戏侧的 `vote.ack.reason` 保留游戏引擎语义，如 `wrong_round`、`duplicate`、`full`。

Bot 每连接默认每秒最多提交 30 条 `vote.cast`；超过后收到可重试的 `protocol.rate_limited`，不会断开长连接。可通过 `THCHAOS_MAX_CASTS_PER_SECOND` 调整。

游戏断线：服务器标记房间离线、广播 `game.offline`，Bot 对新票立即提示不可用。游戏重连并发送 `game.sync` 后，Bot 重新收到当前快照。连接被替换、Token 错误、序号回退等错误会关闭连接；限流和内部错误可按 `retryable` 重试。

## 安全

生产环境只暴露 TLS 443，Token 通过环境变量或本地配置注入；游戏 Token 与 Bot Token 分离。Bot 只允许配置的群映射到房间，`voter_id` 不传原始 QQ 号。
