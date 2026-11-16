-- 演示前事件归档：把 N 分钟前的业务待处置事件归档为已关闭，保持处置队列清爽。
-- 用法：docker exec -i poc-postgres psql -U demo -d demo < scripts/archive-events.sql
update events set status = 'CLOSED', updated_at = now()
where status = 'PENDING' and risk_level in ('high','medium','low')
  and created_at < now() - interval '30 minutes';
