# 架构与设计说明

## 一句话

一个 Python 标准库写的单文件 HTTP 服务，只读地采集本机状态、渲染一个页面，并把 9 个固定命令包成按钮。它位于链路之外。

```
   浏览器 http://127.0.0.1:8090
        │  GET /            GET /api/status        POST /api/action, /api/stop
        ▼
   ┌──────────────────────── ops_panel.py ────────────────────────┐
   │  collect_status()                                            │
   │    ├─ fetch_health()      → 观测代理 /health                  │
   │    ├─ parse_server_status()/launchd_states() → 只读命令输出    │
   │    ├─ port_listeners() / all_listeners()     → lsof + pgrep    │
   │    ├─ recent_records()   → 逐请求 JSONL（只取尾部 400 行）      │
   │    └─ read_kv()          → 两个 .conf                         │
   │  Handler  → HTML 页面 / JSON / 白名单动作 / 优雅退出             │
   └──────────────────────────────────────────────────────────────┘
        │ 只读旁路，不代理、不转发、不修改
        ▼
   推理服务 (8080)  ←  观测代理 (8083)  ←  客户端
```

面板与推理链路之间**没有数据面依赖**：面板挂了，推理照常；推理挂了，面板照常显示「未监听」。

## 三个接口

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 单页 HTML，内含 CSS 与 JS，无外部资源、无 CDN、无前端框架 |
| `/api/status` | GET | 一次返回全部状态 JSON；页面每 15 秒拉一次 |
| `/api/action` | POST | `{"key":"server.status","token":"..."}`，只接受白名单内的 key |
| `/api/stop` | POST | `{"token":"..."}`，先回 200，0.8 秒后从独立线程 `shutdown()` |

不存在其他路由；`/favicon.ico` 返回 204。所有响应带 `Cache-Control: no-store` 与 `X-Content-Type-Options: nosniff`。

## 采集设计

- **不缓存、不落库**：每次 `/api/status` 现场采集，避免面板自己变成另一个状态源。
- **只读日志**：`recent_records()` 用 `deque(f, maxlen=400)` 读尾部，不会把整份 JSONL 读进内存；取不到就返回空数组，页面显示「暂无记录」。
- **不用 `ps`**：部分 macOS 会话里 `ps` 会被系统拦截（`operation not permitted`），进程信息用 `pgrep -fl .` + `lsof -nP` 拿。
- **UDP 语义**：UDP 没有 LISTEN 态，只列有明确本地端口的绑定；`*:*`（未绑定端口）与 `A->B`（已连接对端）都排除，并单独统计数量。
- **IPv4/IPv6 合并**：按 `(协议, 端口, PID, 暴露面)` 去重，双栈在同一行标注。
- **服务识别是「提示」不是「结论」**：只按已知端口表和命令行关键字做字符串匹配，不扫描端口、不发包探测。请以自己的实际部署为准。

## 白名单动作

`ACTIONS` 在进程启动时固定为 `key -> (label, argv, timeout, need_confirm, hint)`。浏览器只传 `key`，服务端查表拿 argv，`subprocess.run(argv, shell=False, timeout=..., env=...)`。

- argv 里不含任何来自请求的字符串；
- `env` 会移除 `PYTHONPATH`、设置 `NO_PROXY`，避免本地回环被代理劫持；
- 超时返回 124 与明确提示，不会让请求永久挂住；
- 每次动作写一条审计日志（时间、key、返回码、耗时、输出前 200 字）。

要加动作，就在源码里加一项 `ACTIONS`——**这是有意的摩擦**：任何新动作都必须经过一次代码提交和发布门禁，而不是运行时可配置。

## 为什么单文件、零依赖

这台机器的主业是跑模型，不是跑运维平台。一个 700 行左右、只用标准库、能被 `python3 ops_panel.py` 直接跑起来的文件，比引入 Flask + 前端构建链 + 常驻守护进程更合适：装得少、坏得少、断网可用、审得完。页面 CSS/JS 内联，是为了不引入任何外部资源和 CDN 请求。

## 版本

`__version__`（`ops_panel.py`）、`VERSION`、`CHANGELOG.md` 三者必须一致，由 `scripts/prepublish_check.py` 在 push 前强制校验。
