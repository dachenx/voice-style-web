import { useEffect, useRef, useState } from 'react';
import { ApiError, messageOf, request } from './api';
import { exportOperation, updateExport, useExportOperation } from './operationCache';
import type { VersionCatalogue } from './VersionPanel';

type ExportRecord = { id: string; project_id?: string; request_sha256?: string; available: boolean; created_at?: string; is_simulated?: boolean;
  pause_ms?: number; audio?: { duration_ms: number }; segments?: { version: { comparison: string } }[] };
// ExportPanel sends all ExportInput fields explicitly, with integer pauses/revisions and
// lowercase UUIDs. Match the backend canonical JSON (sorted keys, ASCII escapes).
async function requestFingerprint(body: string): Promise<string> {
  const value = JSON.parse(body);
  const sorted = { ...value, selections: Object.fromEntries(Object.entries(value.selections).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) };
  const canonical = JSON.stringify(Object.fromEntries(Object.keys(sorted).sort().map(key => [key, sorted[key]])))
    .replace(/[\u007f-\uffff]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}
export default function ExportPanel({ projectId, catalog, disabled, onSelectAudio }: {
  projectId: string; catalog: VersionCatalogue; disabled: boolean; onSelectAudio: () => void;
}) {
  const [records, setRecords] = useState<ExportRecord[]>([]);
  const [error, setError] = useState('');
  const { pause, pending, busy, completed, error: operationError, notice } = useExportOperation(projectId);
  const [ack, setAck] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const active = useRef(false);
  const historyGeneration = useRef(0);
  const base = `/projects/${projectId}/exports`;
  const token = `${catalog.comparison_token}:${catalog.paragraphs.map(p => p.selection?.revision ?? 0).join(',')}`;
  const selected = catalog.paragraphs.map(p => p.versions.find(v => v.id === p.selection?.version_id));
  const missing = selected.some(v => !v?.available);
  const mismatch = selected.some(v => v && v.comparison !== 'matching');
  useEffect(() => {
    const abort = new AbortController();
    const generation = ++historyGeneration.current;
    request<ExportRecord[]>(base, { signal: abort.signal }).then(data => {
      if (abort.signal.aborted || generation !== historyGeneration.current) return;
      setRecords(data); setError('');
      const current = exportOperation(projectId);
      if (current.busy || !current.pending) return;
      const pendingBody = current.pending.body;
      // The stored body was created by this component; hash failures never release it.
      void (async () => {
        try {
          const requestId = JSON.parse(pendingBody).request_id;
          const match = data.find(record => record.id === requestId && record.project_id === projectId && record.available);
          if (!match?.request_sha256) return;
          const fingerprint = await requestFingerprint(pendingBody);
          const latest = exportOperation(projectId);
          if (abort.signal.aborted || generation !== historyGeneration.current || latest.busy || latest.pending?.body !== pendingBody) return;
          if (fingerprint !== match.request_sha256) {
            updateExport(projectId, { error: '历史中同一请求的内容不一致，仍需核对；原请求已保留。' }); return;
          }
          updateExport(projectId, { pending: null, error: '', notice: '已从导出历史核实保存，可下载音频与来源记录。' });
        } catch { /* Keep the original request for an explicit retry. */ }
      })();
    }).catch(cause => { if (!abort.signal.aborted && generation === historyGeneration.current) setError(`导出历史读取失败：${messageOf(cause)}`); });
    return () => abort.abort();
  }, [base, projectId, refresh, completed]);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);
  const lastToken = useRef(token);
  // A changed comparison invalidates acknowledgement; the control is keyed by token below.
  const acknowledged = lastToken.current === token && ack;
  const blocked = disabled || busy || !!pending || !catalog.confirmation_current;
  async function create(paragraph: string | null, retry = false) {
    const current = exportOperation(projectId);
    if (current.busy || (!retry && current.pending) || (retry && !current.pending)) return;
    const body = retry && current.pending ? current.pending.body : JSON.stringify({
      request_id: crypto.randomUUID(), adapter_id: catalog.adapter_id,
      comparison_token: catalog.comparison_token,
      selections: Object.fromEntries(catalog.paragraphs.map(p => [p.id, p.selection?.revision ?? 0])),
      paragraph_id: paragraph, pause_ms: paragraph ? 0 : pause, acknowledge_mismatch: acknowledged,
    });
    historyGeneration.current += 1;
    updateExport(projectId, { pending: { body, paragraph }, busy: true, error: '', notice: '' }); setError('');
    try {
      const result = await request<ExportRecord>(base, { method: 'POST', body }, 60000);
      updateExport(projectId, { pending: null, completed: exportOperation(projectId).completed + 1, notice: '导出已保存，可下载音频与来源记录。' });
      if (!active.current) return;
      historyGeneration.current += 1;
      setRecords(old => [result, ...old.filter(item => item.id !== result.id)]);
    } catch (cause) {
      const rejected = cause instanceof ApiError && ['invalid_input', 'export_inputs_changed', 'export_unconfirmed', 'export_empty', 'export_missing', 'export_acknowledgement', 'export_too_long', 'export_source_changed'].includes(cause.code);
      updateExport(projectId, {
        ...(rejected ? { pending: null } : {}),
        error: rejected ? `导出未创建：${messageOf(cause)} 请修正后重新导出。`
          : `导出结果需要核对：${messageOf(cause)} 可刷新历史，或重试同一请求。`,
      });
    } finally { updateExport(projectId, { busy: false }); }
  }
  return <section className="paragraph-versions" aria-label="拼接与导出">
    <h3>拼接与导出</h3>
    <ol aria-label="拼接操作步骤">
      <li><strong>1. 选择音频</strong>：每段选一个试听满意的版本。<button className="text-button" onClick={onSelectAudio}>选择或更换拼接音频</button></li>
      <li><strong>2. 调整顺序</strong>：在上方“高级分段编辑与历史”拖动段落或点击上移、下移，再点“保存并确认文案与顺序”。</li>
      <li><strong>3. 设置停顿并导出</strong>：核对下方清单，设置新增静音，导出后可先试听再下载。这里只拼接本项目已生成的段落音频。</li>
    </ol>
    {!catalog.paragraphs.length && <p className="field-hint">尚无可拼接段落：先填写文案并生成，再为每段选用音频。</p>}
    <p className="audio-hint">按服务器已保存的段落顺序，使用各段已选用版本。保留原声音量和停顿；新增静音只插在段落之间。导出不代表声音验收通过。</p>
    <ol>{catalog.paragraphs.map((p, i) => <li key={p.id}>第 {i + 1} 段：{selected[i] ? `版本 ${selected[i]!.number} · ${selected[i]!.is_simulated ? '模拟测试音' : '真实生成'} · ${selected[i]!.comparison === 'matching' ? '匹配已保存稿' : '过时或无法核定'}` : '尚未选用'}
      {selected[i]?.stale_reasons.map(reason => <p className="field-hint" key={reason}>{reason}</p>)}
      <button className="text-button" disabled={blocked || !selected[i]?.available || (selected[i]?.comparison !== 'matching' && !acknowledged)}
        onClick={() => void create(p.id)}>导出第 {i + 1} 段 WAV</button></li>)}</ol>
    <label>段间新增静音（毫秒，0–2000）<input type="number" min={0} max={2000} step={10} value={pause} disabled={busy || !!pending}
      onChange={e => updateExport(projectId, { pause: Number(e.target.value) })} /></label>
    {mismatch && catalog.confirmation_current && <label key={token}><input type="checkbox" checked={acknowledged} disabled={busy} onChange={e => { lastToken.current = token; setAck(e.target.checked); }} />我已核对，仍导出过时或无法核定的选用版本</label>}
    {!catalog.confirmation_current && <p className="field-hint">请先确认已保存文案，再创建导出。</p>}
    {missing && <p className="field-hint">整篇缺少可用音频：第 {selected.map((v, i) => !v?.available ? i + 1 : null).filter(Boolean).join('、')} 段。请点击“选择或更换拼接音频”补齐；已就绪段可单独导出。</p>}
    {disabled && <p className="field-hint">请先保存文案并刷新版本，核对最新顺序和选用。</p>}
    <div className="task-actions"><button className="button secondary" disabled={blocked || !catalog.paragraphs.length || missing || (mismatch && !acknowledged) || !Number.isInteger(pause) || pause < 0 || pause > 2000}
      onClick={() => void create(null)}>导出整篇 WAV</button>
      <button className="text-button" disabled={busy} onClick={() => setRefresh(v => v + 1)}>刷新导出历史</button>
      {pending && <button className="text-button" disabled={busy} onClick={() => void create(pending!.paragraph, true)}>重试同一导出请求</button>}</div>
    {pending && <p className="field-hint">有待核对的导出请求，已固定当时的输入，新导出暂不可用。可重试同一请求或刷新历史；本窗口切项目或刷新组件会保留，刷新整个页面前请先核对历史。</p>}
    {operationError && <p role="alert" className="error-box">{operationError}</p>}
    {error && <p role="alert" className="error-box">{error}</p>}
    <p role="status" aria-label="导出操作状态">{notice}</p>
    <h4>已保存导出</h4>
    {!records.length && <p>尚无已保存导出。</p>}
    {records.map(r => <article className="version-card" key={r.id}>
      <p>{r.created_at ? new Date(r.created_at).toLocaleString('zh-CN') : r.id} · {r.is_simulated ? '含模拟测试音，不朗读文案' : '导出记录'} · {r.audio ? `${(r.audio.duration_ms / 1000).toFixed(3)} 秒` : ''}</p>
      {r.segments?.some(s => s.version.comparison !== 'matching') && <p className="field-hint">导出时包含过时或无法核定版本。</p>}
      <p className="audio-hint">固定导出快照，后续改稿和换选用不会修改此文件。</p>
      {r.available ? <div><audio controls preload="none" aria-label="试听拼接结果" src={`/api${base}/${r.id}/audio`} /><div className="task-actions"><a className="text-button" href={`/api${base}/${r.id}/audio`} download>下载 WAV</a>
        <a className="text-button" href={`/api${base}/${r.id}/manifest`} download>下载来源记录 JSON</a></div></div> : <p role="alert">导出文件不可用，历史记录保留，请核对本地文件。</p>}
    </article>)}
  </section>;
}
