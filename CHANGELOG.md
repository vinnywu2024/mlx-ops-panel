# Changelog

本项目使用语义化版本（Semantic Versioning）：`MAJOR.MINOR.PATCH`。

## [1.0.0] - 2026-11-14

首个公开版本。

### 新增

- `ops_panel.py`：零第三方依赖的本地 Web 面板，仅使用 Python 标准库，只绑 `127.0.0.1`。
- 状态总览：推理服务状态、当前加载模型、观测代理健康度、token 预算与排队、提示语注入开关、监听端口汇总。
- 本机监听端口全览：TCP LISTEN 与有明确本地端口的 UDP 绑定，展示绑定地址、暴露面、PID、进程、命令行片段与服务识别；支持「只看本链路 / 只看对外暴露」筛选，IPv4+IPv6 合并为一行。
- 最近请求观测：读取逐请求 JSONL，展示真实 prompt token、`cached_tokens`、输出 token、耗时、前缀 hash、计数方式与截断/排队/注入标记。
- 固定白名单动作（9 项）：服务状态、已装模型、发一句测试、重启推理服务，以及代理状态、运行时配置、最近 10 条、代理自检、重启代理。浏览器只传动作 key，服务端 `subprocess` 用 argv 列表且 `shell=False`。
- 页面内「关闭面板并退出」：二次确认 + 一次性 token，先回 200 再延迟 `shutdown()`，保证浏览器收得到响应。
- `panel.sh`：前台启动 / `status` / `stop` / `restart`，用 `pgrep` + `lsof` 定位进程，不误杀占用同端口的其他进程。
- `launch-panel.command`：macOS 双击启动并打开浏览器。
- 全量配置化：所有机器相关值通过 `PANEL_*` 环境变量注入，模板见 `config.example.env`；本机真实值放 `config.env`（已被 `.gitignore` 排除）。
- 发布门禁 `scripts/prepublish_check.py` 与 `.githooks/pre-push`：自动拦截硬编码本机路径、个人标识、凭据、日志与版本不一致。

### 安全

- 只绑回环；Host 头校验；所有 POST 同时校验 `Origin` 与进程内一次性 token，防跨站触发。
- 不提供停止推理服务、模型切换等破坏性动作；不接收来自浏览器的命令文本。

### 说明

- 本版本从一台 Apple Silicon Mac 上的实际运维小工具整理而来，已移除全部本机专属路径、账户名、标签与内部文档引用。
