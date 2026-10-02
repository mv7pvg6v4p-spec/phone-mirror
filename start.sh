#!/bin/bash
# Phone Mirror launcher: ./start.sh [start|stop|status|log]
cd "$(dirname "$0")" || exit 1

url() { python3 -c "import json;print(json.load(open('mirror.state.json'))['url'])" 2>/dev/null; }

case "${1:-start}" in
  start)
    python3 server.py --stop >/dev/null 2>&1
    python3 server.py --detach
    sleep 3
    u="$(url)"
    if [ -n "$u" ]; then echo "Phone Mirror -> $u"; else
      echo "failed to start, last log lines:"; tail -8 mirror.log; exit 1; fi
    ;;
  stop)   python3 server.py --stop ;;
  status) u="$(url)"; [ -n "$u" ] && curl -s "${u}api/status" | python3 -m json.tool || echo "not running" ;;
  log)    tail -n 40 mirror.log ;;
  *)      echo "usage: $0 [start|stop|status|log]"; exit 1 ;;
esac
