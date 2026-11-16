-- 阶段模型改为现场实际的六个环节：入场签到 / 宣读纪律 / 初步评审 / 详细评审 / 澄清讨论 / 收尾宣读
-- 旧值映射：PREP→SIGNIN，SUMMARY/CLOSE→REPORT，其余保持不变
BEGIN;

ALTER TABLE phase_snapshots DROP CONSTRAINT IF EXISTS phase_snapshot_known_phase;

UPDATE phase_snapshots SET phase_id = 'SIGNIN' WHERE phase_id = 'PREP';
UPDATE phase_snapshots SET phase_id = 'REPORT' WHERE phase_id IN ('SUMMARY', 'CLOSE');
UPDATE events         SET phase_id = 'SIGNIN' WHERE phase_id = 'PREP';
UPDATE events         SET phase_id = 'REPORT' WHERE phase_id IN ('SUMMARY', 'CLOSE');
UPDATE eval_sessions  SET current_phase = 'SIGNIN' WHERE current_phase = 'PREP';
UPDATE eval_sessions  SET current_phase = 'REPORT' WHERE current_phase IN ('SUMMARY', 'CLOSE');

ALTER TABLE phase_snapshots ADD CONSTRAINT phase_snapshot_known_phase
  CHECK (phase_id IN ('SIGNIN','DISCIPLINE','PRELIM','DETAIL','CLARIFY','REPORT'));

COMMIT;
