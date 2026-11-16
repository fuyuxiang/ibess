CREATE OR REPLACE FUNCTION supervision_channel(value text) RETURNS text
        LANGUAGE SQL IMMUTABLE AS $$
          SELECT CASE WHEN value IN ('camera-real-01','cam-ezviz') THEN 'cam-ezviz'
            WHEN value ~ '^(camera-|cam)[0-9]+$' THEN 'cam' || coalesce(nullif(ltrim(regexp_replace(value,'^(camera-|cam)','','g'),'0'),''),'0')
            ELSE value END
        $$;
        CREATE OR REPLACE FUNCTION supervision_scope(src text, at_time timestamptz, hint text DEFAULT NULL)
        RETURNS jsonb LANGUAGE SQL STABLE AS $$
          WITH bindings AS (
            SELECT session_id,project_id,room_id,source_id,created_at,active FROM phase_bindings
            UNION ALL
            SELECT b.session_id,b.project_id,b.room_id,c.source_id,c.created_at AT TIME ZONE 'UTC',c.active AND b.active
              FROM supervision_channels c JOIN phase_bindings b USING(session_id)
          ), candidates AS (
            SELECT b.session_id,b.project_id,b.room_id,b.source_id,p.name project_name,r.name room_name,
              coalesce(snap.phase_id,s.current_phase) phase_id, snap.enabled_scenes
            FROM bindings b JOIN eval_sessions s USING(session_id)
              JOIN business_projects p USING(project_id)
              JOIN business_rooms r ON r.room_id=b.room_id AND r.project_id=b.project_id
              LEFT JOIN LATERAL (SELECT phase_id,enabled_scenes FROM phase_snapshots x
                WHERE x.session_id=b.session_id AND x.effective_at <= at_time ORDER BY x.effective_at DESC LIMIT 1) snap ON true
            WHERE supervision_channel(b.source_id)=supervision_channel(src)
              AND (hint IS NULL OR hint IN ('session-001','unbound','') OR b.session_id=hint)
              AND b.created_at <= at_time + interval '1 second'
              AND (s.start_at IS NULL OR s.start_at AT TIME ZONE 'UTC' <= at_time)
              AND (s.end_at IS NULL OR at_time < s.end_at AT TIME ZONE 'UTC')
              AND ((b.active AND s.status IN ('running','active')) OR (s.status='closed' AND s.end_at IS NOT NULL))
          ) SELECT CASE WHEN count(*)=1 THEN (jsonb_agg(to_jsonb(candidates)))->0 ELSE NULL END FROM candidates
        $$;
