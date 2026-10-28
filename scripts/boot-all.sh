#!/bin/zsh
# 本机常驻服务的统一开机入口（launchd 调用）
#
# 顺序：① 先拉起 8790 端口服务（管理后台 + 托管网关 + 内嵌网关，单端口）
#      ② 健康检查通过后再确认管理后台 UI 可用
#      ③ 最后按顺序拉起 EXTRA_SERVICES 里的其它常驻服务
#
# 幂等：端口已在监听就跳过，重复触发不会互杀。
#
# 为什么必须在这里 unset 代理变量：上游 copilot.tencent.com 直连可达，而
# AI 会话注入的本地代理端口会随会话死亡，导致网关全部出站 Connection refused
# （2026-10-28 16:08 事故）。这里剥干净，与系统代理设置无关。
#
# 为什么必须 unset PYTHONPATH：会话注入的 sitecustomize 会拦截 Path.mkdir()，
# 导致 main.py 启动即崩（2026-10-27 事故）。

set -u

PROJECT="$HOME/projects/Workbuddy-WB2api"
PY="$HOME/.pyenv/shims/python3"
LOG="$PROJECT/logs/boot-all.log"

mkdir -p "$PROJECT/logs"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

port_up() { /usr/sbin/lsof -tnP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
http_ok() { curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 3 "$1" 2>/dev/null | grep -qE '^(200|401|403|405)$'; }

# 关键点：脚本必须「持有」服务进程（wait），既不能 & 完就退出，也不能用 exec 提前替换掉后续步骤。
# 脚本一退出就会被 KeepAlive 重新拉起；若每次只是发现「端口已在监听」就退，会形成每 10 秒一次的
# 空转循环并把日志灌满。用 wait 让脚本与服务同生命周期：服务活着 job 就活着，服务崩溃脚本才返回。
start_8790() {
    log "步骤①：8790 端口服务"
    if port_up 8790; then
        # 端口已被别的实例占着（例如手动跑的那份）：不抢不杀，转为看护模式，
        # 每 5 秒探一次，等它退了自己接管，避免两个入口互相残杀。
        log "  端口 8790 已被占用，进入看护模式"
        local waited=0
        while port_up 8790; do
            sleep 5
            waited=$((waited + 5))
            [ $((waited % 60)) -eq 0 ] && log "  仍在等待端口释放（已 ${waited}s）"
        done
        log "  端口已释放，接管启动"
    fi

    cd "$PROJECT" || { log "  目录不存在，中止"; return 1; }
    env -u PYTHONPATH -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
        -u http_proxy -u https_proxy -u all_proxy -u no_proxy -u AGY_PROXY \
        "$PY" main.py --host 127.0.0.1 --port 8790 &
    MAIN_PID=$!
    log "  已启动 main.py，pid=$MAIN_PID"

    local i
    for i in $(seq 1 30); do
        port_up 8790 && break
        sleep 1
    done
    port_up 8790 && log "  端口就绪 (127.0.0.1:8790)" || log "  端口未就绪，继续观察"

    log "步骤②：管理后台 UI 健康检查"
    for i in $(seq 1 30); do
        http_ok "http://127.0.0.1:8790/admin" && break
        sleep 1
    done
    http_ok "http://127.0.0.1:8790/admin" \
        && log "  /admin 可用" || log "  /admin 未就绪（服务可能仍在初始化）"
}

# ---- 其它常驻服务（按需追加，格式："名称|检查端口|工作目录|命令行"）----
EXTRA_SERVICES=(
    # "aiteam-api|8000|$HOME/projects/aiteam|/Users/mars/.pyenv/shims/python3 -m uvicorn aiteam.api.app:create_app --host 127.0.0.1 --port 8000 --factory"
)

start_8790

idx=0
for entry in "${EXTRA_SERVICES[@]:-}"; do
    idx=$((idx + 1))
    [ -z "$entry" ] && continue
    name="${entry%%|*}";     rest="${entry#*|}"
    port="${rest%%|*}";      rest="${rest#*|}"
    dir="${rest%%|*}";       cmd="${rest#*|}"

    log "步骤③.$idx：附加服务 $name"
    if [ -n "$port" ] && port_up "$port"; then
        log "  端口 $port 已在监听，跳过"
        continue
    fi
    ( cd "$dir" 2>/dev/null && env -u PYTHONPATH -u HTTP_PROXY -u HTTPS_PROXY \
        -u http_proxy -u https_proxy $=cmd >>"$LOG" 2>&1 & ) 2>/dev/null
    log "  已触发启动：$name（端口 $port）"
done

log "步骤④：持有 8790 进程（pid=$MAIN_PID），服务存活期间本脚本不退出"
wait "$MAIN_PID" 2>/dev/null
log "8790 进程已退出 (pid=$MAIN_PID)，脚本结束，由 launchd KeepAlive 重新拉起"
exit 0
