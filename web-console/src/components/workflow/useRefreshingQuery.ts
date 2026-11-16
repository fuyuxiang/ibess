import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkflowStore } from '../../store/useWorkflowStore';
import { createRefreshQueue } from './refresh-queue';

export function useRefreshingQuery<T>(key: string, load: (signal: AbortSignal) => Promise<T>, enabled = true) {
  const revision = useWorkflowStore(s => s.revision);
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce(n => n+1), []);
  const loader = useRef(load); loader.current = load;
  const queue = useRef<ReturnType<typeof createRefreshQueue<T>> | null>(null);
  const trigger = useRef(`${revision}:${nonce}`);
  const [state, setState] = useState<{key:string; data:T|null; error:string; loading:boolean; refreshing:boolean; updatedAt:string|null}>(
    {key, data:null, error:'', loading:enabled, refreshing:false, updatedAt:null});
  useEffect(() => {
    let hasData = false;
    setState({key,data:null,error:'',loading:enabled,refreshing:false,updatedAt:null});
    if (!enabled) return;
    const boundLoad = loader.current;
    const current = createRefreshQueue<T>({
      load: boundLoad,
      data(value) { hasData = true; setState(s => ({...s,data:value,error:'',updatedAt:new Date().toISOString()})); },
      error(error) { setState(s => ({...s,error:error instanceof Error ? error.message : '数据同步失败，请重试'})); },
      loading(active) { setState(s => ({...s,loading:active && !hasData,refreshing:active})); },
    });
    queue.current = current;
    void current.refresh();
    return () => { current.dispose(); if (queue.current===current) queue.current=null; };
  }, [key,enabled]);
  useEffect(() => {
    const next = `${revision}:${nonce}`;
    if (trigger.current !== next) { trigger.current=next; void queue.current?.refresh(); }
  }, [revision,nonce]);
  return {data:state.key===key ? state.data : null,error:state.key===key ? state.error : '',
    loading:state.key!==key ? enabled : state.loading,refreshing:state.refreshing,updatedAt:state.updatedAt,reload};
}
