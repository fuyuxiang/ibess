-- 语音关键词词表：按现场实际环节分为三类（宣读纪律 / 澄清 / 倾向性）
BEGIN;

ALTER TABLE sensitive_lexicon ADD COLUMN IF NOT EXISTS category varchar(32);

INSERT INTO sensitive_lexicon (word, risk_level, enabled, note, category) VALUES
  ('评标纪律',       'medium', true, '宣读纪律环节：纪律告知', 'discipline'),
  ('承诺书',         'medium', true, '宣读纪律环节：签署承诺', 'discipline'),
  ('回避',           'medium', true, '宣读纪律环节：利害关系回避', 'discipline'),
  ('保密义务',       'medium', true, '宣读纪律环节：保密要求', 'discipline'),
  ('通讯工具',       'medium', true, '宣读纪律环节：手机集中保管', 'discipline'),
  ('统一保管',       'low',    true, '宣读纪律环节：手机寄存', 'discipline'),
  ('签字确认',       'low',    true, '宣读纪律环节：签到与确认', 'discipline'),

  ('澄清',           'low',    true, '澄清讨论：提出澄清', 'clarify'),
  ('澄清函',         'medium', true, '澄清讨论：书面澄清', 'clarify'),
  ('补正',           'low',    true, '澄清讨论：材料补正', 'clarify'),
  ('答复',           'low',    true, '澄清讨论：澄清答复', 'clarify'),
  ('异议',           'medium', true, '澄清讨论：现场异议', 'clarify'),
  ('不一致',         'low',    true, '澄清讨论：文件前后不一致', 'clarify'),
  ('补充说明',       'low',    true, '澄清讨论：补充说明', 'clarify'),

  ('倾向',           'high',   true, '倾向性发言：明示偏好', 'tendency'),
  ('照顾',           'high',   true, '倾向性发言：要求照顾', 'tendency'),
  ('关照',           'high',   true, '倾向性发言：请求关照', 'tendency'),
  ('内定',           'high',   true, '倾向性发言：结果已定', 'tendency'),
  ('给个面子',       'high',   true, '倾向性发言：人情施压', 'tendency'),
  ('都打高分',       'high',   true, '倾向性发言：诱导评分', 'tendency'),
  ('打低一点',       'high',   true, '倾向性发言：诱导压分', 'tendency'),
  ('别给太高',       'high',   true, '倾向性发言：诱导压分', 'tendency'),
  ('明显不行',       'medium', true, '倾向性发言：贬损性评价', 'tendency'),
  ('最好这家',       'high',   true, '倾向性发言：指定中标人', 'tendency'),
  ('压低',           'medium', true, '倾向性发言：压分', 'tendency'),
  ('抬高',           'medium', true, '倾向性发言：抬分', 'tendency')
ON CONFLICT (word) DO UPDATE
  SET risk_level = EXCLUDED.risk_level,
      note = EXCLUDED.note,
      category = EXCLUDED.category,
      enabled = true;

UPDATE sensitive_lexicon SET category = 'other' WHERE category IS NULL;

COMMIT;
