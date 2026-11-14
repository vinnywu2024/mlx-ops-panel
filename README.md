# MLX Ops Panel · 端侧模型运维面板

> 给一台跑本地大模型的机器装一块「玻璃」：**只读优先、零第三方依赖、只绑回环**。
> 看状态、看端口、看逐请求 token 观测，把常用运维命令收进固定白名单按钮。

**v1.0.0 · MIT License · Python 3.9+ · 在 Apple Silicon / macOS 上开发验证**

---

## 它解决什么问题

本地跑推理服务时，状态散落在好几个地方：服务起没起、当前加载的是哪个模型、代理的 token 预算还剩多少、前缀缓存有没有命中、机器上到底哪些端口在监听、有没有哪个服务绑到了所有网卡。想确认一下就得敲一串命令，还得记住每个命令的坑。

这个面板把这些信息收进一个本地网页，并且**不改动任何现有链路**：它不代理推理流量、不接管服务进程、不注册开机自启，只是读日志、读配置、调用已有的只读命令。开发它的直接动因是在一台 16GB 统一内存的 Apple Silicon 机器上做端侧模型实验——内存紧张、首轮 prefill 慢、工具调用容易把上下文撑爆，所以格外需要一块「不用思考就能看清楚」的玻璃。

## 能看到什么

| 区域 | 内容 |
| --- | --- |
| 状态卡片 | 关键端口监听情况、当前加载模型、观测代理健康度、token 预算与排队、提示语注入开关、监听端口汇总 |
| 监听端口全览 | 全部 TCP LISTEN + 有明确本地端口的 UDP 绑定；端口 / 协议 / 绑定地址 / 暴露面 / PID / 进程 / 归属 / 服务识别 / 命令行片段；可按「只看本链路」「只看对外暴露」筛选；IPv4 + IPv6 同一端口合并标「双栈」 |
| 最近请求观测 | 逐请求 JSONL 里的真实 prompt token、`cached_tokens`、输出 token、耗时、前缀 hash、计数方式，以及截断 / 限输出 / 排队 / 提示语注入标记 |
| 白名单动作 | 9 个固定按钮（见下表），带超时与二次确认，输出直接显示在页面上，并写审计日志 |
| 关闭面板 | 页面内二次确认后优雅退出，先返回 200 再 shutdown；也可以用 `./panel.sh stop` 或前台启动时 Ctrl-C |

> 判断前缀缓存是否命中看 **cached** 列，不要只看 hash。首轮 `cached≈0` 是正常冷 prefill，耗时偏长不是故障。

## 它不是什么

- **不是**推理服务、反向代理或模型管理器，不替代 `mlx-lm` / Ollama / llama.cpp 等任何运行时。
- **不是**公网可用的运维后台：没有登录、没有账号体系、只绑 `127.0.0.1`，**不要**改绑 `0.0.0.0`，也不要用反向代理暴露到局域网。
- **刻意不做**停止推理服务、模型切换、修改配置等破坏性动作。白名单只放读命令和必要的重启。

## 快速开始

零第三方依赖，标准库即可运行。

```bash
git clone https://github.com/<owner>/mlx-ops-panel.git
cd mlx-ops-panel
cp config.example.env config.env      # 可选：按自己的部署改
./panel.sh                            # 前台启动，另开终端用 ./panel.sh status / stop
# 浏览器打开 http://127.0.0.1:8090
```

macOS 上也可以直接双击 `launch-panel.command`：它会启动面板并自动打开浏览器，关掉那个终端窗口即停。

```bash
./panel.sh status     # 看状态
./panel.sh stop       # 停止，释放端口
./panel.sh restart    # 先停再前台启动
make check            # 发布前门禁自检
```

### 依赖前提（请先看这段）

面板默认面向「用 `mlxctl` 管理推理服务 + 一个前置观测代理 + 逐请求 JSONL 日志」的部署。如果你没有这些组件：

- 面板**仍然能启动**，端口全览、服务识别照常工作；
- 依赖外部命令的卡片会显示占位符 `–`，点击对应按钮会返回明确的失败提示，不会静默跳过；
- 可以用 `PANEL_MLXCTL` / `PANEL_PROXYCTL` 指向你自己的脚本，或者按 `docs/ARCHITECTURE.md` 里的接口约定补一层适配。

想先看效果又不装任何东西，可以直接起面板看「本机监听端口全览」——那部分只依赖系统自带的 `lsof` 和 `pgrep`。

## 配置

所有与具体机器有关的值都走环境变量，写在 `config.env`（已由 `.gitignore` 排除，**永远不入库**）。`panel.sh` 会逐行解析并 export；刻意不使用 `source`，避免执行配置文件里的任意命令。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PANEL_LAB` | `~/mlx-lab` | 配置与日志目录 |
| `PANEL_PORT` | `8090` | 面板端口；绑定地址固定 `127.0.0.1` |
| `PANEL_PYTHON` | `python3` | 解释器 |
| `PANEL_SCRIPT` | 同目录 `ops_panel.py` | 面板代码路径 |
| `PANEL_MLXCTL` | `~/mlx-lab/bin/mlxctl` | 推理服务运维命令 |
| `PANEL_PROXYCTL` | `~/mlx-lab/bin/proxyctl` | 观测代理运维命令 |
| `PANEL_HEALTH_URL` | `http://127.0.0.1:8083/health` | 代理健康接口 |
| `PANEL_RECORD` | `~/mlx-lab/logs/proxy-8083.jsonl` | 逐请求观测日志 |
| `PANEL_SERVER_CONF` / `PANEL_RUNTIME_CONF` | 同目录下两个 `.conf` | 只读展示用 |
| `PANEL_AUDIT` | `~/mlx-lab/logs/ops-panel-audit.jsonl` | 操作审计（只记输出前 200 字） |
| `PANEL_LAUNCHD_LABELS` | 空 | launchd 标签，逗号分隔；每台机器不同，默认不猜 |
| `PANEL_WATCHED_PORTS` | `8080,8081,8083,8090` | 状态卡片关注的端口 |

完整注释见 `config.example.env`。

## 安全边界

- 只绑 `127.0.0.1`；Host 头白名单校验。
- 所有 POST 同时校验 `Origin` 与**进程内一次性 token**（每次启动随机生成，只注入本机页面 HTML），跨站页面受同源策略限制读不到它，无法触发重启或关闭。
- 动作在服务端启动时固定为 argv 列表，浏览器只传 `key`；`subprocess` 一律 `shell=False`，不拼 shell、不接受命令文本。
- 关闭接口先回 200、再延迟 `shutdown()`，保证浏览器收得到响应。
- 不读取、不回传任何模型权重或对话正文之外的信息；审计日志只记输出前 200 字符。

详见 `docs/SECURITY.md`。**这个项目没有鉴权，安全性完全依赖「只绑回环」这一条，请勿突破它。**

## 目录结构

```
.
├── ops_panel.py              # 面板主体：采集 / 页面 / HTTP API（单文件，标准库）
├── panel.sh                  # start / status / stop / restart
├── launch-panel.command      # macOS 双击启动
├── config.example.env        # 配置模板（入库）
├── config.env                # 本机真实配置（不入库）
├── VERSION / CHANGELOG.md    # 版本
├── LICENSE                   # MIT
├── Makefile                  # check / run / status / stop
├── scripts/prepublish_check.py   # 发布门禁
├── .githooks/pre-push        # push 前自动拦截漏脱敏
└── docs/
    ├── ARCHITECTURE.md       # 架构、数据流、API 约定
    ├── SECURITY.md           # 威胁模型与已知限制
    └── MAINTAINING.md        # 生产 / 脱敏 / 发布如何协调，迭代流程
```

## 迭代与发布

**代码只有一份，差异只在配置里**——本机跑的就是仓库这份代码，靠 `config.env` 注入机器相关值，不存在「内部版」和「开源版」两份要同步的源码。每次改动后：

```bash
# 1. 改代码，本机 ./panel.sh 实测
# 2. 发布门禁：扫本机绝对路径、邮箱、凭据、私有标签、日志泄漏、版本一致性
make check
# 3. 更新 VERSION / CHANGELOG.md，提交并打 tag
git add -A && git commit -m "feat: ..."
git tag -a v1.0.1 -m "v1.0.1"
git push && git push --tags
```

第一次 clone 后执行一次 `git config core.hooksPath .githooks`，push 前就会自动跑门禁。完整流程与「现网平滑切换」说明见 **`docs/MAINTAINING.md`**。

## 许可

[MIT](LICENSE) —— 可自由使用、复制、修改、合并、发布、分发、再授权乃至商用，只需保留版权与许可声明。作者不对使用后果作任何担保。

如果你希望连署名义务都不要、彻底放弃版权，可以把 LICENSE 换成 CC0 或 The Unlicense；MIT 是「主流、企业法务普遍接受」这一意义上的最宽松选择。
