#!/bin/bash
# 安装「系统级开机自启」LaunchDaemon：开机即运行，不等用户登录。
#
# 为什么必须 sudo 手工跑一次：
#   macOS 27 上，从 AI 会话/脚本提权的进程（osascript with administrator privileges）
#   写入 /Library/LaunchDaemons 会 EACCES（连 /usr/local/bin 都写不了）。
#   在用户自己的 Terminal 里用 sudo 执行则不受此限制。
#
# 用法：sudo bash /Users/mars/projects/Workbuddy-WB2api/scripts/install-boot-daemon.sh

set -e

SRC="/Users/mars/projects/Workbuddy-WB2api/scripts/com.mars.workbuddy2api.daemon.plist"
DST="/Library/LaunchDaemons/com.mars.workbuddy2api.plist"
DAEMON_PLIST="/Library/LaunchDaemons/com.mars.workbuddy2api.plist"
AGENT_PLIST="/Users/mars/Library/LaunchAgents/com.mars.workbuddy2api.plist"
LABEL="com.mars.workbuddy2api"

if [ "$(id -u)" -ne 0 ]; then
    echo "❌ 需要 root 权限，请这样执行："
    echo "   sudo bash $0"
    exit 1
fi

echo "① 安装守护配置 → $DST"
install -o root -g wheel -m 644 "$SRC" "$DST"

echo "② 卸载用户级 Agent（避免两个入口互相抢端口）"
if [ -f "$AGENT_PLIST" ]; then
    launchctl bootout "gui/$(stat -f%u /Users/mars)/$LABEL" 2>/dev/null || true
    rm -f "$AGENT_PLIST"
    echo "   已移除 $AGENT_PLIST"
else
    echo "   未安装用户级 Agent，跳过"
fi

echo "③ 注册系统守护（system 域）"
launchctl bootout "system/$LABEL" 2>/dev/null || true
launchctl bootstrap system "$DAEMON_PLIST"
launchctl enable "system/$LABEL"

echo "④ 等待启动并做健康检查"
for i in $(seq 1 30); do
    if curl -s --noproxy '*' -o /dev/null --max-time 3 "http://127.0.0.1:8790/admin"; then
        break
    fi
    sleep 1
done

echo "--- 守护状态 ---"
launchctl print "system/$LABEL" | grep -E "state =|pid =|UserName|program =" | head -6
echo "--- 端口与健康 ---"
/usr/sbin/lsof -nP -iTCP:8790 -sTCP:LISTEN | tail -2
curl -s --noproxy '*' -o /dev/null -w "admin=%{http_code}\n" --max-time 5 "http://127.0.0.1:8790/admin"

cat <<'TIP'

✅ 安装完成。此后：
   · 开机（含停在登录界面时）自动启动 8790 管理后台 + 网关
   · 进程崩溃会在 10 秒内被自动拉起
   · 日志：/Users/mars/projects/Workbuddy-WB2api/logs/launchd-*.log

常用命令：
   停止：sudo launchctl bootout system/com.mars.workbuddy2api
   启动：sudo launchctl bootstrap system /Library/LaunchDaemons/com.mars.workbuddy2api.plist
   重启：sudo launchctl kickstart -k system/com.mars.workbuddy2api
TIP
