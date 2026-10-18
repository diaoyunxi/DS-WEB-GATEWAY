# DS-Web-Gateway

浏览器自动化驱动 DeepSeek 网页版，对外提供标准 OpenAI 接口，复用网页版免费额度。
设计与实现依据见 [`方案.md`](方案.md)。

**核心机制**：模型的联网搜索/网页阅读能力即数据通道——模型抓取部分①的公网 URL
完成上行（工具调用/答案）与下行（工具结果/协议），网关**不解析模型输出**。

## 模块

| 模块 | 说明 |
|---|---|
| `common/` | WS 消息与信封 schema、共享配置（token、密钥、超时、长度限制） |
| `server/`（①） | 公网面 `/docs` `/push` `/result` `/probe` + WS 服务 + 管理面（仅内网）+ 浏览器集群 + 会话管理 + Stealth + 信封编解码/分片 + pending_call |
| `gateway/`（②） | OpenAI 端点 `/v1/chat/completions` `/v1/models` + WS 客户端 + messages→prompt 渲染 |

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # 然后按需修改
```

## 配置 cookies

导出 DeepSeek 网页版登录 cookies 为 Playwright cookie 数组 JSON
（`[{"name": "...", "value": "...", "domain": ".deepseek.com", "path": "/"}, ...]），
保存为 `cookies.json`（已在 .gitignore）。过期后人工更换。

## 部署①（常驻公网）

**命名 Cloudflare Tunnel + 自有域名**（quick tunnel URL 每次重启变，不可用）：

```bash
cloudflared tunnel --url http://127.0.0.1:8443 run <tunnel-name>
```

①优先部署在 CN 可达性好的位置（抓取器出口在 CN）。

## 运行

```bash
# 终端 1：①（公网面 8443 / 管理面 8444，管理面仅内网）
python -m server.main

# 终端 2：②（对调用方 8080）
python -m gateway.main
```

启动日志会打印随机生成的 `path_token` 与 `ws_secret`（未在 .env 指定时）；
②通过 `WS_URL` 或默认本机地址拨入①。

调用示例：

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <GATEWAY_API_KEYS>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-web","messages":[{"role":"user","content":"你好"}]}'
```

工具循环为标准 OpenAI `tools` 循环：模型经 `/push` 上报 `tool_call` → ② SSE 下发
`tool_calls`（finish_reason=tool_calls）→ 调用方本地执行 → 下一轮请求携带
`role:tool` → ②经 WS 回传 → ①唤醒 `/result` → 模型同轮继续。

## 阶段 0 门禁（务必先做）

唯一核心未知项是"页面内容是否进入模型上下文"。①配置好隧道后运行：

```bash
python scripts/poc_echo_test.py --probe-base https://<你的域名> --token <path_token>
```

返回 PASS 后再按阶段 1–5 推进（见方案 §12）。FAIL 时先查①的
`GET http://127.0.0.1:8444/admin/logs` 确认 probe 是否被抓取。

## 管理面（仅内网）

```bash
curl http://127.0.0.1:8444/admin/stats            # 总览
curl http://127.0.0.1:8444/admin/sessions         # 活跃线程
curl http://127.0.0.1:8444/admin/logs?n=50        # 抓取遥测（IP/UA）
curl http://127.0.0.1:8444/admin/pending          # pending_call
curl http://127.0.0.1:8444/admin/alerts           # 风控告警
curl -X POST http://127.0.0.1:8444/admin/selector-check   # 选择器自检
```

## 已知边界

- 强依赖 DeepSeek 抓取器行为与网页 DOM 结构，改版即需修 `server/config.py` 选择器（告警驱动）；
- 答案经信封整达，无 token 级流式；
- 模型跑题/不抓取不兜底（既定决策），靠监控人工介入；
- 详见方案 §10。
