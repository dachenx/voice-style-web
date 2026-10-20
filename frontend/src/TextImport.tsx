import { useEffect, useRef, useState } from 'react';
import { messageOf, request } from './api';

type Preview = { text: string; characters: number; warnings: string[] };

export default function TextImport({ baseline, disabled, onApply }: {
  baseline: string; disabled: boolean; onApply: (text: string) => string | null;
}) {
  const controller = useRef<AbortController | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [name, setName] = useState('');
  const [sourceBaseline, setSourceBaseline] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => () => controller.current?.abort(), []);
  const changed = !!preview && baseline !== sourceBaseline;
  async function read(file: File) {
    controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    setError(''); setNotice(''); setPreview(null); setName(file.name); setSourceBaseline(baseline);
    if (file.size > 2 * 1024 * 1024 || !file.size) { setError('文件不能为空，且不能超过 2 MiB。'); return; }
    setLoading(true);
    try {
      const value = await request<Preview>(`/text-import?filename=${encodeURIComponent(file.name)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file, signal: abort.signal,
      });
      if (!abort.signal.aborted) setPreview(value);
    } catch (cause) { if (!abort.signal.aborted) setError(messageOf(cause)); }
    finally { if (!abort.signal.aborted) setLoading(false); }
  }
  return <section aria-label="导入文案" className="text-import">
    <label>导入 TXT / Word 文案<input type="file" accept=".txt,.docx" disabled={disabled || loading}
      onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void read(file); }} /></label>
    <p className="audio-hint">支持 UTF-8 TXT（或带 BOM 的 UTF-16）、Word .docx，最大 2 MiB。旧 .doc 请另存为 .docx。文件仅发送到本机服务解析，不保存原件。</p>
    {loading && <p role="status">正在提取正文…</p>}
    {error && <p role="alert">{error}</p>}
    {preview && <div aria-label="导入预览">
      <p>{name} · 提取 {[...preview.text].length} 字符。确认后只替换当前段朗读稿，其他段与原稿保持不变；不会自动保存或生成。</p>
      {preview.warnings.map(warning => <p key={warning} className="audio-hint">{warning}</p>)}
      <label>导入文字预览<textarea aria-label="导入文字预览" rows={5} style={{ width: '100%' }} value={preview.text}
        disabled={disabled} onChange={event => setPreview({ ...preview, text: event.target.value })} /></label>
      {[...preview.text].length > 80 && <p className="field-hint">万先生 v0.11 每段最多80字符；请在生成前手工分段或缩短，不会自动截断。</p>}
      {changed && <p role="alert">当前文案已改变，请重新选择文件后核对，避免覆盖新修改。</p>}
      <button className="button secondary" disabled={disabled || changed} onClick={() => {
        const problem = onApply(preview.text);
        if (problem) { setError(problem); return; }
        setPreview(null); setError(''); setNotice('已填入当前段，尚未保存。请核对文案后生成。');
      }}>确认替换当前段文案</button>
      <button className="text-button" disabled={disabled} onClick={() => { setPreview(null); setError(''); }}>取消导入</button>
    </div>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
