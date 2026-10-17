export type Project = {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
};

export async function request<T>(path: string, options: RequestInit = {}, timeoutMs = 10000): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
      signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new Error('暂时无法连接本地服务，请确认服务已启动后重试。', { cause: error });
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error?.message ?? '操作未完成，请稍后重试。');
  }
  return response.json() as Promise<T>;
}

export function validateName(value: string): string | null {
  if (/\p{Cc}/u.test(value)) return '项目名称不能包含换行或控制字符。';
  const name = value.trim();
  if (!name) return '请输入项目名称。';
  if ([...name].length > 80) return '项目名称最多 80 个字。';
  return null;
}

export const messageOf = (error: unknown) =>
  error instanceof Error ? error.message : '操作未完成，请稍后重试。';
