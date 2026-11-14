export interface ToolTrace {
  name: string;
  ok: boolean;
}

export function formatToolTrace(trace: ToolTrace): string {
  return trace.ok ? `已调用 ${trace.name}` : `${trace.name} 没有完成`;
}
