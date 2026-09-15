# astrbot_plugin_thchaos

把 THChaos 后端连接到 AstrBot/OneBot QQ 群。插件直接监听群消息，不调用 LLM；投票结果和票数完全采用游戏端消息。

安装：把此目录复制到 AstrBot 的 `data/plugins/astrbot_plugin_thchaos`，按 `_conf_schema.json` 配置后重载插件。后端必须使用 WSS/TLS 和独立 Bot Token；`group_ids` 是唯一允许投票的群白名单。

插件用 `aiohttp` 建立后端 Bot WebSocket，连接失败会指数退避重连。QQ群内每个 `1/2/3` 都会作为一条 `vote.cast` 发送；群消息只对 `vote.snapshot` 做默认 2 秒合并，内部消息仍即时转发。

NapCat 不需要直接连接本后端：按 AstrBot 的 OneBot 适配器配置 NapCat 作为 WebSocket 客户端连接 AstrBot（建议使用反向 WS），由 AstrBot 接收群消息并调用本插件。后端只看见 Bot Token 和 HMAC 伪名，不接触 QQ 原始账号。
