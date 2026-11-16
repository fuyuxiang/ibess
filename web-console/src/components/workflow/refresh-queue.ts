/** 同一查询串行执行；刷新合并，查询切换才取消在途请求。 */
export function createRefreshQueue<T>(options: {
  load: (signal: AbortSignal) => Promise<T>;
  data: (value: T) => void;
  error: (error: unknown) => void;
  loading: (active: boolean) => void;
}) {
  let disposed = false, running = false, pending = false;
  let controller: AbortController | null = null;
  async function refresh(): Promise<void> {
    if (disposed) return;
    if (running) { pending = true; return; }
    running = true;
    controller = new AbortController();
    const current = controller;
    const timer = globalThis.setTimeout(() => current.abort(), 20000);
    options.loading(true);
    try {
      const value = await options.load(current.signal);
      if (!disposed) options.data(value);
    } catch (error) {
      if (!disposed) options.error(error);
    } finally {
      globalThis.clearTimeout(timer);
      running = false;
      if (!disposed) {
        options.loading(false);
        if (pending) { pending = false; void refresh(); }
      }
    }
  }
  return { refresh, dispose() { disposed = true; pending = false; controller?.abort(); } };
}
