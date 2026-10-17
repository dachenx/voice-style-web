import { useEffect, useRef, useState } from 'react';
import { messageOf, request } from './api';
import type { VersionCatalogue } from './VersionPanel';

type ExportRecord = { id: string; available: boolean; created_at?: string; is_simulated?: boolean;
  pause_ms?: number; audio?: { duration_ms: number }; segments?: { version: { comparison: string } }[] };
export default function ExportPanel({ projectId, catalog, disabled }: {
  projectId: string; catalog: VersionCatalogue; disabled: boolean;
}) {
  const [records, setRecords] = useState<ExportRecord[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pause, setPause] = useState(0);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const active = useRef(false);
  const historyGeneration = useRef(0);
  const pending = useRef<{ body: string; paragraph: string | null } | null>(null);
  const base = `/projects/${projectId}/exports`;
  const token = `${catalog.comparison_token}:${catalog.paragraphs.map(p => p.selection?.revision ?? 0).join(',')}`;
  const selected = catalog.paragraphs.map(p => p.versions.find(v => v.id === p.selection?.version_id));
  const missing = selected.some(v => !v?.available);
  const mismatch = selected.some(v => v && v.comparison !== 'matching');
  useEffect(() => {
    const abort = new AbortController();
    const generation = ++historyGeneration.current;
    request<ExportRecord[]>(base, { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted && generation === historyGeneration.current) { setRecords(data); setError(''); }
    }).catch(cause => { if (!abort.signal.aborted && generation === historyGeneration.current) setError(`导出历史读取失败：${messageOf(cause)}`); });
    return () => abort.abort();
  }, [base, refresh]);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);
  const lastToken = useRef(token);
  // A changed comparison invalidates acknowledgement; the control is keyed by token below.
  const acknowledged = lastToken.current === token && ack;
  const blocked = disabled || busy || !catalog.confirmation_current;
  async function create(paragraph: string | null, retry = false) {
    if (busy) return;
    const body = retry && pending.current ? pending.current.body : JSON.stringify({
      request_id: crypto.randomUUID(), adapter_id: catalog.adapter_id,
      comparison_token: catalog.comparison_token,
      selections: Object.fromEntries(catalog.paragraphs.map(p => [p.id, p.selection?.revision ?? 0])),
      paragraph_id: paragraph, pause_ms: paragraph ? 0 : pause, acknowledge_mismatch: acknowledged,
    });
    historyGeneration.current += 1;
    pending.current = { body, paragraph };
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await request<ExportRecord>(base, { method: 'POST', body }, 60000);
      if (!active.current) return;
      historyGeneration.current += 1;
      pending.current = null;
      setRecords(old => [result, ...old.filter(item => item.id !== result.id)]);
      setNotice('导出已保存，可下载音频与来源记录。');
    } catch (cause) {
      if (active.current) setError(`导出结果需要核对：${messageOf(cause)} 可刷新历史，或重试同一请求。`);
    } finally { if (active.current) setBusy(false); }
  }
  return <section className="paragraph-versions" aria-label="拼接与导出">
    <h3>拼接与导出</h3>
    <p className="audio-hint">按服务器已保存的段落顺序，使用各段已选用版本。保留原声音量和停顿；新增静音只插在段落之间。导出不代表声音验收通过。</p>
    <ol>{catalog.paragraphs.map((p, i) => <li key={p.id}>第 {i + 1} 段：{selected[i] ? `版本 ${selected[i]!.number} · ${selected[i]!.is_simulated ? '模拟测试音' : '真实生成'} · ${selected[i]!.comparison === 'matching' ? '匹配已保存稿' : '过时或无法核定'}` : '尚未选用'}
      {selected[i]?.stale_reasons.map(reason => <p className="field-hint" key={reason}>{reason}</p>)}
      <button className="text-button" disabled={blocked || !selected[i]?.available || (selected[i]?.comparison !== 'matching' && !acknowledged)}
        onClick={() => void create(p.id)}>导出第 {i + 1} 段 WAV</button></li>)}</ol>
    <label>段间新增静音（毫秒，0–2000）<input type="number" min={0} max={2000} step={10} value={pause} disabled={busy}
      onChange={e => setPause(Number(e.target.value))} /></label>
    {mismatch && catalog.confirmation_current && <label key={token}><input type="checkbox" checked={acknowledged} disabled={busy} onChange={e => { lastToken.current = token; setAck(e.target.checked); }} />我已核对，仍导出过时或无法核定的选用版本</label>}
    {!catalog.confirmation_current && <p className="field-hint">请先确认已保存文案，再创建导出。</p>}
    {missing && <p className="field-hint">整篇缺少可用选用音频，请在上方逐段选用；已就绪段可单独导出。</p>}
    {disabled && <p className="field-hint">请先保存文案并刷新版本，核对最新顺序和选用。</p>}
    <div className="task-actions"><button className="button secondary" disabled={blocked || missing || (mismatch && !acknowledged) || !Number.isInteger(pause) || pause < 0 || pause > 2000}
      onClick={() => void create(null)}>导出整篇 WAV</button>
      <button className="text-button" disabled={busy} onClick={() => setRefresh(v => v + 1)}>刷新导出历史</button>
      {pending.current && <button className="text-button" disabled={busy || disabled} onClick={() => void create(pending.current!.paragraph, true)}>重试同一导出请求</button>}</div>
    {error && <p role="alert" className="error-box">{error}</p>}
    <p role="status" aria-label="导出操作状态">{notice}</p>
    <h4>已保存导出</h4>
    {!records.length && <p>尚无已保存导出。</p>}
    {records.map(r => <article className="version-card" key={r.id}>
      <p>{r.created_at ? new Date(r.created_at).toLocaleString('zh-CN') : r.id} · {r.is_simulated ? '含模拟测试音，不朗读文案' : '导出记录'} · {r.audio ? `${(r.audio.duration_ms / 1000).toFixed(3)} 秒` : ''}</p>
      {r.segments?.some(s => s.version.comparison !== 'matching') && <p className="field-hint">导出时包含过时或无法核定版本。</p>}
      <p className="audio-hint">固定导出快照，后续改稿和换选用不会修改此文件。</p>
      {r.available ? <div className="task-actions"><a className="text-button" href={`/api${base}/${r.id}/audio`} download>下载 WAV</a>
        <a className="text-button" href={`/api${base}/${r.id}/manifest`} download>下载来源记录 JSON</a></div> : <p role="alert">导出文件不可用，历史记录保留，请核对本地文件。</p>}
    </article>)}
  </section>;
}
