/** 对齐完整句段，避免在60秒边界悄悄丢掉跨界原话。 */
export function semanticWindow<T extends {start_ms:number;end_ms:number}>(segments:T[],requestedStart:number,duration:number) {
  const ordered = [...segments].sort((a,b)=>a.start_ms-b.start_ms);
  const first = ordered.find(s=>s.end_ms>requestedStart);
  if (!first || requestedStart>=duration) throw Error('窗口开始时间超出有效转写范围');
  if (first.end_ms-first.start_ms>60000) throw Error('起始句段超过60秒，请先拆分该长句段再分析');
  const selected = ordered.filter(s=>s.start_ms>=first.start_ms && s.end_ms<=Math.min(duration,first.start_ms+60000));
  return {start:first.start_ms,end:Math.max(...selected.map(s=>s.end_ms)),segments:selected};
}
