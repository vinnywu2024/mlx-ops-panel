# 生产、脱敏与发布如何协调

> 这份文档回答一个很实际的问题：**项目会迭代，本机正在跑的那份和 GitHub 上公开的那份，怎么保证不打架？**

## 结论：只维护一份代码，差异全部交给配置

不要维护「内部版」和「开源版」两套源码。两套源码必然漂移：改了内部版忘了同步、发布时漏脱敏、某天想合并发现 diff 里全是本机路径——这是最常见的失控方式。

本项目采用 **单一真源（single source of truth）**：

```
        ┌───────────────────────────────────────────────┐
        │  Git 仓库（唯一代码源，公开）                    │
        │  ops_panel.py / panel.sh / docs / 门禁脚本       │
        └───────────────────┬───────────────────────────┘
                            │
           ┌────────────────┴────────────────┐
           ▼                                 ▼
   ┌────────────────┐              ┌────────────────────┐
   │ 本机「生产」    │              │ GitHub「发布」       │
   │ git pull 后跑   │              │ git push + tag      │
   │ ./panel.sh      │              │                    │
   │ + config.env    │              │ 同一份 commit       │
   └────────────────┘              └────────────────────┘
        本机专属值只存在于 config.env（.gitignore 排除）
```

- **代码**：仓库里一份，本机 `git pull` 就是升级，没有「同步」这一步。
- **机器差异**：目录、端口、命令路径、launchd 标签、日志文件名，全部是 `PANEL_*` 环境变量，写在本机 `config.env`。改配置不改代码，配置永远不入库。
- **脱敏**：不是发布时的一次性动作，而是**编码约束**——源码里根本不允许出现本机专属值。门禁负责在 push 前把偶发疏忽拦下来。

## 文件归属表

| 文件 | 入库 | 作用 |
| --- | --- | --- |
| `ops_panel.py`、`panel.sh`、`launch-panel.command` | ✅ | 代码，唯一真源 |
| `config.example.env` | ✅ | 配置模板，只有占位值和通用默认值 |
| `config.env` | ❌ | 本机真实值；`.gitignore` 第一行就排除它 |
| `*.jsonl`、`logs/`、`audit/` | ❌ | 运行期产物，可能含本机路径与内网信息 |
| `docs/`、`README.md`、`CHANGELOG.md`、`VERSION` | ✅ | 公开文档 |
| `scripts/prepublish_check.py`、`.githooks/pre-push` | ✅ | 门禁，随仓库分发 |
| 本机实验报告、运维手册 PDF、基准数据 | ❌ | 属于另一个项目，含实测数据与内部路径，不进这个仓库 |

## 发布门禁

```bash
make check     # 等价于 python3 scripts/prepublish_check.py
```

检查的是 **git 已跟踪的文件**（真正会被 push 出去的内容），逐行扫描：本机绝对路径、邮箱、各类凭据形态、具体的 launchd 标签；同时确认 `config.env` / `*.jsonl` / `*.log` / 备份文件没有被跟踪，以及 `VERSION` / `__version__` / `CHANGELOG.md` 三者版本一致、LICENSE 完整、`.gitignore` 生效。

第一次 clone 后启用一次：

```bash
git config core.hooksPath .githooks
```

之后每次 `git push` 都会自动跑门禁，漏脱敏直接 push 失败。注意 hook 随仓库分发但**不会自动启用**，每台机器要自己执行一次（git 的安全设计）。

## 迭代流程

```
1. 在仓库里改代码 → ./panel.sh 本机实测（用的就是这份代码）
2. make check
3. 更新 VERSION 与 CHANGELOG.md，写清楚改了什么
4. git add -A && git commit -m "feat: ..."
5. git tag -a v1.0.1 -m "v1.0.1"     # 版本号见下方规则
6. git push && git push --tags
7. 在 GitHub 上由 tag 建 Release（可选）
8. 其他机器：git pull && ./panel.sh restart   # config.env 不动
```

版本规则：`MAJOR.MINOR.PATCH`。改了接口/破坏配置兼容 → MAJOR；加功能且向后兼容 → MINOR；修 bug / 改文案 → PATCH。只有 MIT 许可下的公开仓库，不必为每个小修复都发版，但**打 tag 的三处版本必须一致**，否则门禁会拦。

## 从「旧的本机脚本」平滑切过来

如果已有一份硬编码路径的旧面板正在 8090 跑，不要直接覆盖。按这个顺序切：

1. 先把本机真实值写进 `config.env`（尤其是 `PANEL_LAB`、`PANEL_MLXCTL`、`PANEL_PROXYCTL`、launchd 标签、日志文件名）。
2. 用另一个端口做并行验证：`PANEL_PORT=8092 ./panel.sh`，确认页面、端口全览、白名单动作、关闭按钮都正常。
3. 停掉旧进程（只停面板进程，不要动推理服务）。
4. `./panel.sh` 用默认端口起来，旧脚本保留一段时间作为回退，但不再更新。
5. 稳定后把桌面快捷方式指向仓库里的 `launch-panel.command`。

这样切换期间推理链路一次都不用重启。

## 场景速查

| 想做的事 | 怎么做 | 要不要发版 |
| --- | --- | --- |
| 换模型 / 换目录 / 换端口 | 改 `config.env` + 重启面板 | 不用 |
| 加一个新按钮 | 改 `ACTIONS`，走完整流程 | 要（MINOR） |
| 修页面文案 / 样式 | 改 `ops_panel.py`，走完整流程 | 要（PATCH），也可攒着一起发 |
| 临时调一个参数试效果 | 命令行前缀 `PANEL_PORT=8092 ./panel.sh` | 不用 |
| 写了一堆本机实验结论 | 放**另一个项目**的文档里 | 不进这个仓库 |

## 红线

1. **永远不要** `git add -f config.env`，也不要把日志、审计文件、备份加进仓库。
2. **永远不要**在源码或文档注释里写本机绝对路径、账户名、邮箱、私有标签；要举例就用 `~/` 与占位符。
3. **不要**为图省事在源码里加 `if is_my_machine:` 之类的分支——那等于重新引入第二份代码。
4. **不要**跳过 `make check` 直接 push；hook 只是兜底，不是保险箱。
5. 门禁规则本身放宽时，要在 commit message 里说明理由，别默默删规则。
