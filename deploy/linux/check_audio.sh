#!/usr/bin/env bash
# 验证：音频事件能否自动关联到录音与转写
set -euo pipefail
T=$(python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip(chr(61)))")
H=$(printf %s "$T" | sha256sum | cut -d" " -f1)
docker exec poc-postgres psql -U demo -d demo -q -c \
  "insert into admin_sessions(token_hash,account_id,expires_at,csrf_token) values ('$H',1,now()+interval '1 hour','v4') on conflict do nothing;" >/dev/null

EID=$(docker exec poc-postgres psql -U demo -d demo -t -A -c \
  "select event_id from events where camera_id='camera-007' order by created_at desc limit 1" | tr -d '\r')
echo "event=$EID"

code=$(curl -s --cookie "eval_admin_session=$T" "http://127.0.0.1:8000/api/events/$EID" -o /tmp/e.json -w '%{http_code}')
echo "detail=$code  bytes=$(wc -c < /tmp/e.json)"

python3 - <<'PY'
import json
d = json.load(open('/tmp/e.json'))
c = d.get('context_evidence') or {}
a = c.get('audio') or []
print('audio 条数', len(a))
for x in a[:2]:
    segs = x.get('segments') or []
    print(' title:', x.get('title'), '| 片段:', len(segs), '| 播放:', x.get('playback_url'))
    for s in segs[:3]:
        print('   ', (s.get('speaker') or '未知'), ':', (s.get('text') or '')[:38])
PY

docker exec poc-postgres psql -U demo -d demo -q -c "delete from admin_sessions where csrf_token='v4';" >/dev/null
