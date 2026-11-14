#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MLX Ops Panel —— 只读优先的端侧模型本地运维面板。

给一台跑本地 LLM 推理的机器装一块「玻璃」：看状态、看端口、看逐请求
token 观测，并把常用运维命令收进固定白名单按钮。它不代理推理流量，
也不替代任何现有服务。

设计原则：
  1. 不替换、不改动已有推理服务 / 观测代理链路，只在旁边加一个只读页面。
  2. 只用 Python 标准库，零第三方依赖；只绑 127.0.0.1。
  3. 按钮走固定命令白名单，subprocess 传 argv 列表、shell=False，
     不接受任何来自浏览器的命令文本，也不拼 shell。
  4. 刻意不提供停止推理服务、模型切换等破坏性动作；页面只提供「关闭自己」。
  5. 所有 POST 带进程内一次性 token 与 Origin 校验，防跨站触发；
     关闭接口先回 200 再延迟 shutdown，保证浏览器收得到响应。

所有与具体机器有关的值（目录、端口、命令路径、launchd 标签、日志名）
都从环境变量读取，见 config.example.env。代码里不含任何本机身份信息。

启动 / 停止：
    ./panel.sh                 # 前台启动
    ./panel.sh status          # 看状态
    ./panel.sh stop            # 终端里停
浏览器：
    http://127.0.0.1:8090  → 页面下方「关闭面板并退出」按钮
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "1.0.0"

# 只绑回环是安全边界，刻意不提供配置成 0.0.0.0 的开关。
HOST = "127.0.0.1"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_path(name: str, default: str) -> Path:
    return Path(env(name, default)).expanduser()


def env_ports(name: str, default: str) -> tuple[int, ...]:
    out = []
    for chunk in env(name, default).replace(" ", "").split(","):
        if chunk.isdigit() and 0 < int(chunk) < 65536:
            out.append(int(chunk))
    return tuple(dict.fromkeys(out))


LAB = env_path("PANEL_LAB", "~/mlx-lab")
PORT = int(env("PANEL_PORT", "8090"))
# 一次性凭据：进程启动时生成，只注入本机页面 HTML。
# 恶意网页受同源策略限制读不到它，因此无法跨站触发任何 POST。
STOP_TOKEN = secrets.token_urlsafe(24)
MLXCTL = str(Path(env("PANEL_MLXCTL", str(LAB / "bin/mlxctl"))).expanduser())
PROXYCTL = str(Path(env("PANEL_PROXYCTL", str(LAB / "bin/proxyctl"))).expanduser())
RECORD = env_path("PANEL_RECORD", str(LAB / "logs/proxy-8083.jsonl"))
SERVER_CONF = env_path("PANEL_SERVER_CONF", str(LAB / "server.conf"))
RUNTIME_CONF = env_path("PANEL_RUNTIME_CONF", str(LAB / "proxy-runtime.conf"))
AUDIT = env_path("PANEL_AUDIT", str(LAB / "logs/ops-panel-audit.jsonl"))
HEALTH_URL = env("PANEL_HEALTH_URL", "http://127.0.0.1:8083/health")
# launchd 标签属于每台机器自己的配置，默认不猜任何用户名。
LABELS = tuple(x for x in env("PANEL_LAUNCHD_LABELS").split(",") if x)
WATCHED_PORTS = tuple(dict.fromkeys(
    env_ports("PANEL_WATCHED_PORTS", "8080,8081,8083,8090") + (PORT,)))

# key -> (按钮文字, argv, 超时秒, 是否需二次确认, 说明)
# argv 在进程启动时固定，浏览器只传 key，永远传不进命令文本。
ACTIONS = {
    "server.status": ("服务状态", [MLXCTL, "status"], 15, False, "只读：配置/端口/PID/实际加载模型"),
    "server.models": ("已装模型", [MLXCTL, "models"], 15, False, "只读：列出本地已下载的权重"),
    "server.health": ("发一句测试", [MLXCTL, "health"], 60, False, "会真发一句短问答并计时；占用推理队列"),
    "server.restart": ("重启推理服务", [MLXCTL, "restart"], 90, True, "会清空 KV 与 prompt cache，之后首轮较慢"),
    "proxy.status": ("代理状态", [PROXYCTL, "status"], 15, False, "只读：版本/后端/预算/排队/记录数"),
    "proxy.config": ("运行时配置", [PROXYCTL, "config"], 15, False, "只读：代理运行时配置的全部键"),
    "proxy.report": ("最近 10 条", [PROXYCTL, "report", "10"], 15, False, "只读：token / cached / 前缀 hash 报表"),
    "proxy.selftest": ("代理自检", [PROXYCTL, "selftest"], 90, False, "会发少量测试请求，验证截断/并发/SSE"),
    "proxy.restart": ("重启代理", [PROXYCTL, "restart"], 60, True, "修改代理配置或换模型后才需要"),
}


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def clean_env() -> dict:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)  # 宿主 shim 会让子进程重导入慢 40 倍
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["no_proxy"] = env["NO_PROXY"]
    return env


def run_cmd(argv: list[str], timeout: int) -> tuple[int, str, float]:
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, env=clean_env())
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip(), round(time.time() - t0, 2)
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）：命令未结束，服务可能正忙。", round(time.time() - t0, 2)
    except Exception as exc:                                   # noqa: BLE001
        return 1, f"执行失败：{exc}", round(time.time() - t0, 2)


def fetch_health() -> dict:
    """禁用代理直连 8083；本地回环常被 HTTP_PROXY 劫持。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(HEALTH_URL, timeout=4) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def read_kv(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:                                          # noqa: BLE001
        pass
    return out


def parse_server_status() -> dict:
    rc, out, _ = run_cmd([MLXCTL, "status"], 12)
    d = {"raw": out, "rc": rc}
    patterns = {
        "configured": r"配置模型\s*:\s*(.+)",
        "loaded": r"实际加载中\s*:\s*(.+)",
        "port": r"端口\s*:\s*(.+)",
        "pid": r"进程\s*:\s*PID\s*(\d+)",
        "launchd": r"launchd\s*:\s*(.+)",
    }
    for k, pat in patterns.items():
        m = re.search(pat, out)
        d[k] = m.group(1).strip() if m else "-"
    return d


def launchd_states() -> dict:
    res = {}
    uid = os.getuid()
    for label in LABELS:
        rc, out, _ = run_cmd(["launchctl", "print", f"gui/{uid}/{label}"], 8)
        m = re.search(r"state\s*=\s*(\S+)", out)
        res[label] = m.group(1) if m else ("未注册" if rc else "未知")
    return res


def port_listeners() -> dict:
    args = ["lsof"]
    for p in WATCHED_PORTS:
        args += ["-iTCP:%d" % p]
    args += ["-sTCP:LISTEN", "-nP"]
    rc, out, _ = run_cmd(args, 8)
    found = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        m = re.search(r":(\d+)\s*\(LISTEN\)", line)
        if m:
            found[int(m.group(1))] = {"command": parts[0], "pid": parts[1]}
    return found


# 常见端口 → (归属标签, 服务说明)。只是解释用，不做端口扫描或连通性探测。
# 具体机器上到底哪个端口跑什么，请用 PANEL_* 环境变量配合部署自行判断。
KNOWN_PORTS = {
    8080: ("常见", "MLX / OpenAI 兼容推理服务常见端口"),
    8083: ("常见", "反向代理或观测代理常见端口"),
    8091: ("常见", "旁路代理常见端口"),
    11434: ("常见", "Ollama 默认端口"),
    8000: ("常见", "本地开发服务常见端口"),
    7000: ("系统", "macOS 控制中心 / AirPlay 接收器"),
    5000: ("系统", "macOS 控制中心 / AirPlay 接收器"),
}
CMD_HINTS = (
    ("mlx_lm", "MLX 推理服务"), ("ops_panel.py", "本运维面板"),
    ("ollama", "Ollama 服务"), ("llama_server", "llama.cpp 服务"),
    ("llamactl", "llamactl 代理"), ("vmlx", "vMLX / MLX Studio"),
    ("Code Helper", "VS Code Helper"), ("Electron", "Electron 桌面应用"),
    ("rapportd", "macOS 隔空投送 / 接力"),
    ("ControlCenter", "macOS 控制中心"), ("node", "Node.js 进程"),
)


def proc_cmdlines() -> dict:
    """ps 在部分会话被系统拦截（operation not permitted），改用 pgrep -fl .。"""
    _, out, _ = run_cmd(["pgrep", "-fl", "."], 10)
    mapping = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            mapping[int(parts[0])] = parts[1].strip()
    return mapping


def identify_service(port: int, command: str, cmdline: str) -> tuple[str, str]:
    if port == PORT:
        return ("本链路", "本运维面板（只读）")
    if port in KNOWN_PORTS:
        return KNOWN_PORTS[port]
    text = ((cmdline or "") + " " + (command or "")).lower()
    for needle, label in CMD_HINTS:
        if needle.lower() in text:
            return ("", label)
    return ("", "未识别（看命令行）")


def all_listeners() -> dict:
    """本机全部 TCP LISTEN + 有明确本地端口的 UDP 绑定。

    每行给出端口 / 协议 / 绑定地址 / PID / 进程 / 完整命令行片段 / 服务识别 /
    是否对所有接口开放。IPv4+IPv6 同一端口合并为一行（标「双栈」）。
    UDP 的 `*:*`（未绑定端口）与 `A->B`（已连接对端）不算监听端口。
    """
    cmds = proc_cmdlines()
    raw = []
    for proto, args in (("TCP", ["-iTCP", "-sTCP:LISTEN"]), ("UDP", ["-iUDP"])):
        _, out, _ = run_cmd(["lsof", "-nP"] + args, 12)
        for line in out.splitlines()[1:]:
            raw.append((proto, line))

    rows, seen = [], {}
    udp_unbound = 0
    for proto, line in raw:
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            i = parts.index(proto)
        except ValueError:
            continue
        if i + 1 >= len(parts):
            continue
        addr = parts[i + 1]
        if "->" in addr:                                   # 已连接对端，不是监听
            continue
        m = re.match(r"^(.*):(\d+)$", addr)
        if not m:
            if proto == "UDP":
                udp_unbound += 1
            continue
        bind, port = m.group(1), int(m.group(2))
        loopback = bind in ("127.0.0.1", "::1", "[::1]") or bind.startswith("127.")
        exposed = not loopback                            # * / 0.0.0.0 / 具体网卡 IP
        norm = "all" if exposed else "loopback"
        pid = int(parts[1]) if parts[1].isdigit() else 0
        ipver = parts[4] if len(parts) > 4 else ""
        key = (proto, port, pid, norm)
        if key in seen:
            if ipver and ipver not in seen[key]["ipvers"]:
                seen[key]["ipvers"].append(ipver)
            continue
        command = parts[0].replace("\\x20", " ").replace("\\ ", " ")
        cmdline = cmds.get(pid, "")
        scope, service = identify_service(port, command, cmdline)
        row = {"proto": proto, "port": port, "bind": bind, "pid": pid,
               "command": command, "cmdline": cmdline[:150],
               "scope": scope, "service": service, "exposed": exposed,
               "ipvers": [ipver] if ipver else []}
        rows.append(row)
        seen[key] = row

    rows.sort(key=lambda r: (r["port"], r["proto"]))
    return {"rows": rows,
            "tcp": sum(1 for r in rows if r["proto"] == "TCP"),
            "udp": sum(1 for r in rows if r["proto"] == "UDP"),
            "exposed": sum(1 for r in rows if r["exposed"]),
            "chain": sum(1 for r in rows if r["scope"] == "本链路"),
            "udp_unbound": udp_unbound}


def recent_records(n: int = 12) -> list[dict]:
    keys = ("ts", "method", "path", "model", "status", "latency_s",
            "prompt_tokens", "cached_tokens", "completion_tokens",
            "system_prefix_hash", "tokenizer", "queued_s",
            "finish_reason", "passthrough")
    rows = []
    try:
        with RECORD.open(encoding="utf-8") as f:
            tail = deque(f, maxlen=400)
        for line in tail:
            try:
                d = json.loads(line)
            except Exception:                                  # noqa: BLE001
                continue
            row = {k: d.get(k) for k in keys}
            a = d.get("actions") or {}
            row["truncate"] = bool(a.get("truncate"))
            row["output_cap"] = bool(a.get("output_cap"))
            row["guard_injected"] = a.get("guard_injected", 0)
            row["hygiene_warn"] = d.get("hygiene_warn") or ""
            rows.append(row)
    except Exception:                                          # noqa: BLE001
        return []
    return list(reversed(rows[-n:]))


def collect_status() -> dict:
    conf = read_kv(SERVER_CONF)
    runtime = read_kv(RUNTIME_CONF)
    ports = port_listeners()
    return {
        "version": __version__,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "panel_port": PORT,
        "health": fetch_health(),
        "server": parse_server_status(),
        "launchd": launchd_states(),
        "launchd_labels": list(LABELS),
        "ports": ports,
        "watched_ports": list(WATCHED_PORTS),
        "listeners": all_listeners(),
        "server_conf": {k: conf.get(k, "") for k in
                        ("MODEL", "HOST", "PORT", "DISABLE_THINKING",
                         "PROMPT_CACHE_BYTES", "PROMPT_CONCURRENCY")},
        "runtime": runtime,
        "guard_enabled": bool(runtime.get("GUARD_PROMPT_FILE")),
        "recent": recent_records(12),
        "actions": [{"key": k, "label": v[0], "confirm": v[3], "hint": v[4]}
                    for k, v in ACTIONS.items()],
    }


def audit(key: str, label: str, rc: int, elapsed: float, output: str) -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "key": key, "label": label, "rc": rc,
                "elapsed_s": elapsed, "output_head": output[:200],
            }, ensure_ascii=False) + "\n")
    except Exception:                                          # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>端侧模型运维面板</title>
<style>
:root{--ink:#1f2328;--muted:#57606a;--line:#d8dee4;--soft:#f6f8fa;
--brand:#1f4fa8;--brand-soft:#eaf1fd;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
header{background:var(--brand-soft);border-bottom:1px solid var(--line);padding:18px 26px}
header h1{margin:0;font-size:20px;color:var(--brand)}
header p{margin:4px 0 0;color:var(--muted);font-size:13px}
main{max-width:1180px;margin:0 auto;padding:22px 26px 60px}
.bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px;
 color:var(--muted);font-size:13px}
button{font:inherit;border:1px solid var(--line);background:#fff;color:var(--ink);
 border-radius:7px;padding:7px 13px;cursor:pointer;transition:.15s}
button:hover{border-color:var(--brand);color:var(--brand);background:var(--brand-soft)}
button.danger{border-color:#ffcecb;color:var(--bad)}
button.danger:hover{background:#fff1f0;border-color:var(--bad);color:var(--bad)}
button:disabled{opacity:.5;cursor:wait}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-bottom:22px}
.card{border:1px solid var(--line);border-radius:9px;padding:13px 15px;background:var(--soft)}
.card h3{margin:0 0 8px;font-size:12px;letter-spacing:.05em;color:var(--muted);font-weight:600}
.card .big{font-size:16px;font-weight:600;word-break:break-all}
.card .sub{font-size:12px;color:var(--muted);margin-top:5px;word-break:break-all}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.pill.ok{background:#dafbe1;color:var(--ok)}
.pill.bad{background:#ffebe9;color:var(--bad)}
.pill.warn{background:#fff8c5;color:var(--warn)}
.pill.mid{background:#eaeef2;color:var(--muted)}
#lsFilters button.active{background:var(--brand-soft);border-color:var(--brand);color:var(--brand);font-weight:600}
h2{font-size:15px;margin:26px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{background:var(--soft);color:var(--muted);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.wrap{white-space:normal;word-break:break-all;max-width:220px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
.hint{font-size:12px;color:var(--muted);margin:8px 0 0}
#btns{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
#out{background:#0d1117;color:#d1d9e0;border-radius:8px;padding:14px;min-height:90px;
 max-height:340px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
 white-space:pre-wrap;word-break:break-word}
footer{border-top:1px solid var(--line);margin-top:34px;padding-top:14px;
 font-size:12px;color:var(--muted)}
code{background:var(--soft);padding:1px 5px;border-radius:4px}
</style></head><body>
<header><h1>端侧模型运维面板 <span id="ver" class="hint"></span></h1>
<p>只读优先 · 不改动已有推理链路 · 只绑 127.0.0.1:__PANEL_PORT__ · 手动启停，不开机自启</p></header>
<main>
  <div class="bar"><span id="ts">载入中…</span><button onclick="refresh()">立即刷新</button>
    <span>每 15 秒自动刷新</span><span id="err"></span></div>
  <div class="cards" id="cards"></div>

  <h2>本机监听端口全览 <span id="lsMeta" class="hint" style="font-weight:400"></span></h2>
  <div class="bar" id="lsFilters">
    <button class="active" onclick="setLsFilter('all',this)">全部</button>
    <button onclick="setLsFilter('chain',this)">只看本链路</button>
    <button onclick="setLsFilter('exposed',this)">只看对外暴露</button>
    <span class="hint">lsof 实时采集，每 15 秒随页面刷新</span>
  </div>
  <div class="scroll"><table><thead><tr>
    <th>端口</th><th>协议</th><th>绑定地址</th><th>暴露面</th>
    <th>进程</th><th class="num">PID</th><th>归属</th><th>服务识别</th><th>命令行</th>
    </tr></thead><tbody id="listeners"></tbody></table></div>
  <p class="hint">「对外暴露」指绑定在 <code>*</code> / <code>0.0.0.0</code> / 非回环地址，
  <b>不等于有风险</b>——macOS 自带的控制中心、隔空投送也在列。本面板刻意只绑
  <code>127.0.0.1</code>；「本链路」只按端口做提示，请以自己的实际部署为准。
  UDP 没有 LISTEN 态，只列有明确本地端口的绑定，未绑定端口与已连接对端已排除。</p>

  <h2>最近请求观测（逐请求日志）</h2>
  <div class="scroll"><table><thead><tr>
    <th>时间</th><th>类型</th><th>状态</th><th class="num">真实 prompt</th>
    <th class="num">cached</th><th class="num">输出</th><th class="num">耗时 s</th>
    <th>前缀 hash</th><th>计数</th><th>标记</th></tr></thead>
    <tbody id="recent"></tbody></table></div>
  <p class="hint">判断前缀缓存是否命中看 <b>cached</b> 列，不要只看 hash（hash 只取前两条 system）。
  首轮 cached≈0 属正常冷 prefill，耗时偏长不是故障。</p>

  <h2>操作（固定白名单，不接收任意命令）</h2>
  <div id="btns"></div>
  <pre id="out">点上面的按钮，输出会显示在这里。</pre>
  <p class="hint">刻意不提供停止推理服务、模型切换等破坏性动作。白名单在服务端启动时固定，
  浏览器只能传动作 key；没有配置对应命令时，按钮会返回明确的失败提示，不会静默跳过。</p>

  <h2>关闭本面板</h2>
  <p class="hint">只停 __PANEL_PORT__ 这个页面进程，<b>已有推理服务不受影响</b>；关完直接退出本页即可。
  下次要用，在仓库目录跑 <code>./panel.sh</code>。</p>
  <button id="stopBtn" class="danger" onclick="stopPanel()">关闭面板并退出</button>

  <footer>数据来源：配置中的 <code>mlxctl</code> / <code>proxyctl</code> / <code>/health</code> /
  逐请求 JSONL。说明见本仓库 <code>README.md</code>。本页不代理任何推理请求。<br>
  关闭方式：本按钮 / 终端 <code>./panel.sh stop</code> / 前台启动时按 Ctrl-C。</footer>
</main>
<script>
let ACTS = [];
const STOP_TOKEN = "__STOP_TOKEN__";
let timer = null;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function pill(txt,good){return '<span class="pill '+(good?'ok':'bad')+'">'+esc(txt)+'</span>';}
function card(title,big,sub){return '<div class="card"><h3>'+esc(title)+'</h3><div class="big">'+big+
  '</div><div class="sub">'+sub+'</div></div>';}
function fmt(n){return n==null?'–':Number(n).toLocaleString('zh-CN');}

let LSFILTER='all';
function setLsFilter(f,btn){
  LSFILTER=f;
  document.querySelectorAll('#lsFilters button').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  applyLsFilter();
}
function applyLsFilter(){
  document.querySelectorAll('#listeners tr[data-scope]').forEach(tr=>{
    const scope=tr.dataset.scope, ex=tr.dataset.exposed==='1';
    const show=LSFILTER==='all'||(LSFILTER==='chain'&&scope==='本链路')||(LSFILTER==='exposed'&&ex);
    tr.style.display=show?'':'none';
  });
}
function renderListeners(L){
  L=L||{rows:[]};
  document.getElementById('lsMeta').textContent='TCP '+(L.tcp||0)+' · UDP '+(L.udp||0)+
    ' · 本链路 '+(L.chain||0)+' · 对外 '+(L.exposed||0)+
    ' · 另有 '+(L.udp_unbound||0)+' 个未绑定端口的 UDP socket 未列出';
  const rows=(L.rows||[]).map(r=>{
    const pill=r.scope==='本链路'?'<span class="pill ok">本链路</span>':
      r.scope==='第三方'?'<span class="pill warn">第三方</span>':
      r.scope?'<span class="pill mid">'+esc(r.scope)+'</span>':'–';
    const face=r.exposed?'<span class="pill warn">对外/指定接口</span>':'<span class="pill ok">仅本机</span>';
    const dual=(r.ipvers&&r.ipvers.length>1)?' <span class="hint">双栈</span>':'';
    return '<tr data-scope="'+esc(r.scope||'')+'" data-exposed="'+(r.exposed?1:0)+'">'+
      '<td class="num"><b>'+esc(r.port)+'</b></td>'+
      '<td>'+esc(r.proto)+dual+'</td>'+
      '<td>'+esc(r.bind)+'</td>'+
      '<td>'+face+'</td>'+
      '<td>'+esc(r.command)+'</td>'+
      '<td class="num">'+esc(r.pid)+'</td>'+
      '<td>'+pill+'</td>'+
      '<td class="wrap">'+esc(r.service)+'</td>'+
      '<td class="wrap"><code>'+esc(r.cmdline||'–')+'</code></td></tr>';
  }).join('');
  document.getElementById('listeners').innerHTML=rows||
    '<tr><td colspan="9">未采集到监听端口（lsof 可能被拦截）</td></tr>';
  applyLsFilter();
}
function render(d){
  document.getElementById('ts').textContent = '更新于 '+esc(d.ts);
  document.getElementById('ver').textContent = d.version?('v'+esc(d.version)):'';
  const h=d.health||{}, s=d.server||{}, rt=d.runtime||{};
  const backendOK = !!h.backend_ok;
  const ld=d.launchd||{}, labels=d.launchd_labels||[];
  const ldText = labels.length ? labels.map(x=>esc(ld[x]||'–')).join(' / ') : '未配置标签';
  const wp=d.watched_ports||[], listening=Object.keys(d.ports||{}).length;
  let cards='';
  cards += card('关键端口',
    (listening?pill(listening+' / '+wp.length+' 监听中',true):pill('未监听',false)),
    '关注端口 '+esc(wp.join(' / ')||'–')+' · launchd '+ldText);
  cards += card('当前加载模型', esc(s.loaded||'–'),
    '配置值：'+esc(s.configured||'–'));
  cards += card('观测代理',
    h.ok?pill('在线 v'+esc(h.version),true):pill('未响应',false),
    '后端 '+(backendOK?'可达':'不可达')+' '+esc(h.backend_ping_ms==null?'':h.backend_ping_ms+' ms'));
  cards += card('预算与排队', esc(h.budget==null?'–':fmt(h.budget))+' token',
    '输出上限 '+esc(rt.MAX_OUTPUT_TOKENS||'不限')+' · 等待 '+esc(h.waiting)+' · 在途 '+esc(h.inflight));
  cards += card('提示语注入',
    d.guard_enabled?pill('已启用',true):pill('未启用',false),
    d.guard_enabled?esc(rt.GUARD_PROMPT_FILE):'由代理侧配置，面板只读不改');
  const L=d.listeners||{};
  cards += card('监听端口总览', (L.tcp||0)+' 个 TCP LISTEN',
    '本链路 '+(L.chain||0)+' 个 · 对外 '+(L.exposed||0)+' 个 · 本面板 '+esc(d.panel_port||'–'));
  document.getElementById('cards').innerHTML=cards;
  renderListeners(L);

  const rows=(d.recent||[]).map(r=>{
    let marks=[];
    if(r.truncate) marks.push('截断');
    if(r.output_cap) marks.push('限输出');
    if(r.queued_s>0) marks.push('排队 '+r.queued_s+'s');
    if(r.guard_injected) marks.push('注入 '+r.guard_injected);
    if(r.hygiene_warn) marks.push(esc(r.hygiene_warn));
    const st=String(r.status||'');
    return '<tr><td>'+esc(String(r.ts||'').slice(5,19))+'</td>'+
      '<td>'+esc(r.passthrough?'透传':(r.method||'')+' /v1/chat')+'</td>'+
      '<td>'+(st==='200'?pill(st,true):esc(st))+'</td>'+
      '<td class="num">'+fmt(r.prompt_tokens)+'</td>'+
      '<td class="num">'+fmt(r.cached_tokens)+'</td>'+
      '<td class="num">'+fmt(r.completion_tokens)+'</td>'+
      '<td class="num">'+esc(r.latency_s==null?'–':r.latency_s)+'</td>'+
      '<td>'+esc(r.system_prefix_hash||'–')+'</td>'+
      '<td>'+esc(r.tokenizer||'–')+'</td>'+
      '<td class="wrap">'+marks.join(' · ')+'</td></tr>';
  }).join('');
  document.getElementById('recent').innerHTML = rows ||
    '<tr><td colspan="10">暂无记录（今天可能还没发过业务请求）</td></tr>';

  ACTS = d.actions||[];
  document.getElementById('btns').innerHTML = ACTS.map(a =>
    '<button class="'+(a.confirm?'danger':'')+'" data-key="'+esc(a.key)+
    '" title="'+esc(a.hint)+'" onclick="act(this)">'+esc(a.label)+'</button>').join('');
}

async function refresh(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    render(await r.json());
    document.getElementById('err').textContent='';
  }catch(e){document.getElementById('err').textContent='状态读取失败：'+e;}
}
async function stopPanel(){
  if(!confirm(`确认关闭运维面板？

只停 __PANEL_PORT__ 这个页面进程，已有推理服务不受影响。
关闭后可直接退出本页。`)) return;
  const btn=document.getElementById('stopBtn'), out=document.getElementById('out');
  btn.disabled=true; out.textContent='正在关闭面板 …';
  if(timer) clearInterval(timer);
  try{
    const r=await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:STOP_TOKEN})});
    const d=await r.json();
    if(!r.ok) throw new Error('HTTP '+r.status+' '+(d.output||''));
    out.textContent=`面板已停止，可以关闭本页面。

${d.output||''}

下次启动： ./panel.sh`;
  }catch(e){
    out.textContent=`关闭请求未收到响应（面板可能已经退出）：${e}

如页面仍可用，请再点一次；或在终端执行：
./panel.sh stop`;
  }
  document.querySelectorAll('#btns button').forEach(b=>b.disabled=true);
  document.getElementById('err').textContent='';
}
async function act(btn){
  const key=btn.dataset.key;
  const meta=ACTS.find(a=>a.key===key)||{};
  if(meta.confirm && !confirm('确认执行「'+meta.label+'」？\n'+meta.hint)) return;
  const out=document.getElementById('out');
  btn.disabled=true; out.textContent='执行中：'+key+' …';
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:key,token:STOP_TOKEN})});
    const d=await r.json();
    out.textContent='$ '+key+'  ['+d.elapsed_s+'s, rc='+d.rc+']\n\n'+(d.output||'(无输出)');
  }catch(e){out.textContent='请求失败：'+e;}
  finally{btn.disabled=false; refresh();}
}
refresh(); timer = setInterval(refresh,15000);
</script></body></html>"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def stop_self(srv) -> None:
    """延迟 0.8s 再停：让 /api/stop 的 200 响应先完整发回浏览器。

    shutdown() 必须从 serve_forever 之外的线程调用；SIGTERM 只作兜底。
    """
    time.sleep(0.8)
    try:
        srv.shutdown()
    except Exception:                                          # noqa: BLE001
        os.kill(os.getpid(), signal.SIGTERM)


class Handler(BaseHTTPRequestHandler):
    server_version = "MlxOpsPanel/" + __version__

    def log_message(self, fmt, *args):          # 只留错误与动作，别刷屏
        if self.path.startswith(("/api/action", "/api/stop")):
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _host_ok(self) -> bool:
        h = (self.headers.get("Host") or "").split(":")[0].lower()
        return h in ("127.0.0.1", "localhost", "::1")

    def _origin_ok(self) -> bool:
        """POST 的 Origin 若存在，必须是本面板自己；配合 token 防跨站触发。"""
        origin = (self.headers.get("Origin") or "").rstrip("/").lower()
        if not origin:                       # 无 Origin 的本地 curl / 脚本
            return True
        return origin in {
            "http://127.0.0.1:%d" % PORT,
            "http://localhost:%d" % PORT,
            "http://[::1]:%d" % PORT,
        }

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        path = url_path(self.path)
        if path == "/":
            page = (PAGE.replace("__STOP_TOKEN__", STOP_TOKEN)
                        .replace("__PANEL_PORT__", str(PORT)))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(collect_status())
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if not self._host_ok() or not self._origin_ok():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        path = url_path(self.path)
        if path not in ("/api/action", "/api/stop"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            raw = self.rfile.read(n) if n else b"{}"
            payload = json.loads(raw[:4096] or b"{}")
        except Exception:                                      # noqa: BLE001
            self._json({"rc": 1, "output": "请求体不是合法 JSON", "elapsed_s": 0}, 400)
            return
        # 所有写操作统一校验一次性 token：跨站页面受同源策略限制读不到页面里的值。
        if not isinstance(payload, dict) or payload.get("token") != STOP_TOKEN:
            self._json({"rc": 1, "output": "token 无效，已拒绝（防跨站触发）", "elapsed_s": 0}, 403)
            return
        if path == "/api/stop":
            audit("panel.stop", "关闭运维面板", 0, 0, "self shutdown after 0.8s")
            self._json({"rc": 0, "label": "关闭运维面板", "elapsed_s": 0,
                        "output": "已释放 127.0.0.1:%d，已有推理服务不受影响。" % PORT,
                        "ts": time.strftime("%H:%M:%S")})
            try:
                self.wfile.flush()
            except Exception:                                  # noqa: BLE001
                pass
            threading.Thread(target=stop_self, args=(self.server,), daemon=True).start()
            return
        key = payload.get("key")
        if key not in ACTIONS:
            self._json({"rc": 1, "output": "不在命令白名单内，已拒绝", "elapsed_s": 0}, 403)
            return
        label, argv, timeout, _, _ = ACTIONS[key]
        rc, output, elapsed = run_cmd(argv, timeout)
        audit(key, label, rc, elapsed, output)
        self._json({"rc": rc, "output": output, "elapsed_s": elapsed,
                    "label": label, "ts": time.strftime("%H:%M:%S")})


def url_path(target: str) -> str:
    from urllib.parse import urlparse
    return urlparse(target).path


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("MLX Ops Panel v%s 已启动： http://%s:%d" % (__version__, HOST, PORT), flush=True)
    print("只绑回环 · 白名单动作 %d 项 · 页面按钮 / ./panel.sh stop / Ctrl-C 可关闭"
          % len(ACTIONS), flush=True)
    print("配置目录：%s%s" % (LAB, "" if LAB.exists() else "（不存在，相关采集会显示 –）"),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print("面板已停止（已有推理服务不受影响）", flush=True)


if __name__ == "__main__":
    main()
