DELETE FROM phase_snapshots
WHERE session_id = 'a7214dea-f3ff-43e8-9eb8-9ab3a21c9ebd'
  AND effective_at >= '2026-10-31 00:00+00';

INSERT INTO phase_snapshots
  (stage_snapshot_id, session_id, project_id, room_id, source_id, phase_id, phase_version,
   effective_at, enabled_scenes, always_on_scenes, assets, rule_versions, exceptions,
   sync_status, actor_id, reason, created_at)
VALUES
  (gen_random_uuid()::text, 'a7214dea-f3ff-43e8-9eb8-9ab3a21c9ebd', '0671ff8f-2abb-420b-95b0-206fec89e46e', '8d25e744-9ef1-48d2-9901-7422935c0f76', 'camera-002', 'OPENING', 20, '2026-10-31 03:30:00+00', '["S10"]', '["S10"]', '[]', '{"S10":"s10-v1"}', '[]', 'applied', 'wyh', '开标记录形成，封闭评标开始', now()),
  (gen_random_uuid()::text, 'a7214dea-f3ff-43e8-9eb8-9ab3a21c9ebd', '0671ff8f-2abb-420b-95b0-206fec89e46e', '8d25e744-9ef1-48d2-9901-7422935c0f76', 'camera-002', 'ATTENDANCE', 21, '2026-10-31 04:35:00+00', '["S01","S04","S10"]', '["S10"]', '[]', '{"S01":"s01-v1","S04":"s04-v1","S10":"s10-v1"}', '[]', 'applied', 'wyh', '专家签到与设备寄存', now()),
  (gen_random_uuid()::text, 'a7214dea-f3ff-43e8-9eb8-9ab3a21c9ebd', '0671ff8f-2abb-420b-95b0-206fec89e46e', '8d25e744-9ef1-48d2-9901-7422935c0f76', 'camera-002', 'PRELIMINARY', 22, '2026-10-31 05:40:00+00', '["S10"]', '["S10"]', '[]', '{"S10":"s10-v1"}', '[]', 'applied', 'wyh', '宣读纪律并开展初步评审', now()),
  (gen_random_uuid()::text, 'a7214dea-f3ff-43e8-9eb8-9ab3a21c9ebd', '0671ff8f-2abb-420b-95b0-206fec89e46e', '8d25e744-9ef1-48d2-9901-7422935c0f76', 'camera-002', 'DETAILED', 23, '2026-10-31 06:45:00+00', '["S02","S03","S06","S10"]', '["S10"]', '[]', '{"S02":"s02-v1","S03":"s03-v1","S06":"s06-v1","S10":"s10-v1"}', '[]', 'applied', 'wyh', '进入详细评审', now());

UPDATE events SET phase_id = NULL WHERE occurred_at >= '2026-10-31 03:30:00';

UPDATE events e
SET phase_id = sub.phase_id
FROM (
  SELECT e2.event_id,
         (SELECT p.phase_id
          FROM phase_snapshots p
          WHERE p.session_id = 'a7214dea-f3ff-43e8-9eb8-9ab3a21c9ebd'
            AND p.effective_at AT TIME ZONE 'UTC' <= e2.occurred_at
          ORDER BY p.effective_at DESC
          LIMIT 1) AS phase_id
  FROM events e2
  WHERE e2.occurred_at >= '2026-10-31 03:30:00'
) sub
WHERE e.event_id = sub.event_id AND sub.phase_id IS NOT NULL;

SELECT phase_id,
       count(*) AS 事件数,
       count(*) FILTER (WHERE risk_level IN ('high', 'medium')) AS 中高风险,
       to_char(min(occurred_at), 'HH24:MI') AS 最早,
       to_char(max(occurred_at), 'HH24:MI') AS 最新
FROM events
WHERE phase_id IS NOT NULL
GROUP BY phase_id
ORDER BY min(occurred_at);

SELECT phase_id, phase_version, to_char(effective_at, 'MM-DD HH24:MI') AS 生效
FROM phase_snapshots
WHERE session_id = 'a7214dea-f3ff-43e8-9eb8-9ab3a21c9ebd'
ORDER BY effective_at DESC LIMIT 5;
