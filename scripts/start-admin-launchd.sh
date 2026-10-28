#!/bin/zsh
# launchd 常驻入口：管理后台 + 内嵌网关（127.0.0.1:8790）
# 说明：
# - 必须 env -u PYTHONPATH：WorkBuddy 会话环境注入的 sitecustomize 会拦截
#   Path.mkdir()，导致 main.py 启动即崩（见 2026-10-27 排障记录）。
# - main.py 自带：依赖自检、旧实例自动接管、uvicorn 子进程看护。
# 日志：logs/launchd-stdout.log / logs/launchd-stderr.log

cd "$HOME/projects/Workbuddy-WB2api" || exit 1

# 剥离全部代理变量：上游 copilot.tencent.com 直连即可；若继承 AI 会话的
# 本地代理端口，代理随会话死亡后网关所有出站请求都会 Connection refused
# （2026-10-28 16:08 故障根因）。
exec /usr/bin/env -u PYTHONPATH \
  -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
  -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
  -u AGY_PROXY \
  "$HOME/.pyenv/shims/python3" main.py --host 127.0.0.1 --port 8790
