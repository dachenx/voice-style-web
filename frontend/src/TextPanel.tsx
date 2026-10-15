import { useEffect, useRef, useState } from 'react';
import { messageOf, request } from './api';
import { pendingDrafts } from './textDraftCache';

export type Paragraph = { id: string; original_text: string; draft_text: string; pronunciation_note: string };
type Content = { source_language: string; target_language: string; register: string; terms: string; paragraphs: Paragraph[] };
type Confirmation = Content & { id: string; revision: number; created_at: string; conversion_warnings: string[] };
export type TextDocument = Content & { revision: number; conversion_warnings: string[]; latest_confirmation: Confirmation | null; confirmation_current: boolean };
const contentOf = (doc: Content): Content => ({ source_language: doc.source_language, target_language: doc.target_language, register: doc.register, terms: doc.terms, paragraphs: doc.paragraphs });
const length = (value: string) => [...value].length;
function validation(doc: Content): string | null {
  if (doc.paragraphs.length > 200) return '最多保存 200 段。';
  if (length(doc.register) > 80 || length(doc.terms) > 2000) return '语体最多 80 字，术语备注最多 2000 字。';
  if (doc.paragraphs.some((p) => length(p.original_text) > 5000 || length(p.draft_text) > 5000 || length(p.pronunciation_note) > 500)) return '每段原稿和草稿各最多 5000 字，读音备注最多 500 字。';
  if (['original_text', 'draft_text'].some((key) => doc.paragraphs.reduce((sum, p) => sum + length(p[key as 'original_text' | 'draft_text']), 0) > 20000)) return '整篇原稿和草稿各最多 20000 字。';
  return null;
}

export default function TextPanel({ projectId }: { projectId: string }) {
  const [doc, setDoc] = useState<TextDocument | null>(() => pendingDrafts.get(projectId)?.doc ?? null);
  const [saved, setSaved] = useState(() => pendingDrafts.get(projectId)?.saved ?? '');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [ack, setAck] = useState(false);
  const [history, setHistory] = useState<Pick<Confirmation, 'id' | 'revision' | 'created_at'>[]>([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [snapshot, setSnapshot] = useState<Confirmation | null>(null);
  const [converterMessage, setConverterMessage] = useState('尚未连接文本转换组件，当前可人工填写草稿。');
  const controller = useRef<AbortController | null>(null);
  const operation = useRef(0);
  const historyOperation = useRef(0);
  const base = `/projects/${projectId}/text`;
  const dirty = !!doc && JSON.stringify(contentOf(doc)) !== saved;
  const invalid = doc ? validation(doc) : null;
  const complete = !!doc?.paragraphs.length && doc.paragraphs.every((p) => p.original_text.trim() && p.draft_text.trim());
  const current = !!doc?.confirmation_current && !dirty;

  useEffect(() => {
    if (doc && dirty) pendingDrafts.set(projectId, { doc, saved });
    else if (doc) pendingDrafts.delete(projectId);
  }, [doc, dirty, projectId, saved]);

  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    const version = ++operation.current;
    void request<TextDocument>(base, { signal: abort.signal }).then((value) => {
      if (abort.signal.aborted || version !== operation.current) return;
      const pending = pendingDrafts.get(projectId);
      setDoc(pending?.doc ?? value); setSaved(pending?.saved ?? JSON.stringify(contentOf(value)));
      if (pending) setNotice('已恢复此项目在本窗口的未保存输入，请保存；如有版本冲突可重新载入。');
    }).catch((cause) => { if (!abort.signal.aborted && version === operation.current) setError(messageOf(cause)); });
    void request<{ converters: unknown[]; message: string }>('/text-converters', { signal: abort.signal }).then((value) => {
      if (!abort.signal.aborted) setConverterMessage(value.converters.length ? '已发现转换组件，本版请人工填写并核对草稿。' : value.message);
    }).catch(() => { /* The editor remains available without capability discovery. */ });
    return () => { abort.abort(); operation.current += 1; };
  }, [base, projectId]);

  function edit(change: (value: TextDocument) => TextDocument) {
    if (!doc || busy) return;
    setDoc(change(doc)); setAck(false); setNotice('');
  }
  async function perform(kind: 'save' | 'confirm' | 'reload') {
    if (!controller.current || busy) return;
    if (kind === 'reload' && dirty && !window.confirm('重新载入服务器版本会丢弃本地未保存文案，是否继续？')) return;
    const abort = controller.current;
    const version = ++operation.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const value = await request<TextDocument>(kind === 'confirm' ? `${base}/confirm` : base, {
        signal: abort.signal,
        method: kind === 'reload' ? 'GET' : kind === 'save' ? 'PUT' : 'POST',
        ...(kind === 'reload' ? {} : { body: JSON.stringify(kind === 'save' ? { ...contentOf(doc!), expected_revision: doc!.revision } : { expected_revision: doc!.revision, acknowledged: true }) }),
      });
      if (abort.signal.aborted || version !== operation.current) return;
      setDoc(value); setSaved(JSON.stringify(contentOf(value))); setAck(false);
      setNotice(kind === 'save' ? '文案已保存，请核对后人工确认。' : kind === 'confirm' ? '文案已确认。' : '已载入服务器版本。');
    } catch (cause) {
      if (!abort.signal.aborted && version === operation.current) setError(`${messageOf(cause)} 本地输入已保留；如有版本冲突，可重新载入服务器版本。`);
    } finally { if (!abort.signal.aborted && version === operation.current) setBusy(false); }
  }
  async function loadHistory(id?: string) {
    const abort = controller.current;
    if (!abort) return;
    const version = ++historyOperation.current;
    try {
      if (id) {
        const value = await request<Confirmation>(`${base}/confirmations/${id}`, { signal: abort.signal });
        if (!abort.signal.aborted && version === historyOperation.current) setSnapshot(value);
      } else {
        const value = await request<{ confirmations: typeof history }>(`${base}/confirmations`, { signal: abort.signal });
        if (!abort.signal.aborted && version === historyOperation.current) { setHistory(value.confirmations); setSnapshot(null); setHistoryLoaded(true); }
      }
    } catch (cause) { if (!abort.signal.aborted && version === historyOperation.current) setError(messageOf(cause)); }
  }
  return <section className="reference-panel text-panel" aria-label="文案工作台" aria-busy={busy}>
    <div className="reference-heading"><div><span className="eyebrow">TEXT WORKSPACE</span><h2>文案与人工确认</h2><p>普通话原稿 → 武汉话草稿 → 人工确认。请核对原意、数字、专名与读音。</p></div></div>
    {error && <p className="error-box" role="alert">{error}</p>}
    <div className="text-actions"><button className="text-button" disabled={busy} onClick={() => void perform('reload')}>重新载入服务器版本</button></div>
    {!doc ? <p className="muted">正在读取文案…</p> : <>
      <div className="text-language">源语言：普通话（zh-CN） <span>目标：武汉话（wuhan）</span><span>已保存版本：{doc.revision}</span></div>
      <fieldset disabled={busy} className="text-fields">
        <div className="text-pair"><label>语体<input value={doc.register} onChange={(e) => edit((d) => ({ ...d, register: e.target.value }))} /></label><label>术语备注<textarea value={doc.terms} onChange={(e) => edit((d) => ({ ...d, terms: e.target.value }))} /></label></div>
        <div className="converter-note"><button className="button secondary" disabled>自动改写（暂不可用）</button><p>{converterMessage || '尚未连接文本转换组件，当前可人工填写草稿。'} 人工填写可用；复制原稿不等于方言转换。</p></div>
        {doc.paragraphs.map((paragraph, index) => <article className="text-paragraph" key={paragraph.id} data-paragraph-id={paragraph.id} aria-label={`第 ${index + 1} 段`}>
          <div className="text-paragraph-heading"><h3>第 {index + 1} 段</h3><div className="text-actions">
            <button className="text-button" disabled={index === 0} onClick={() => edit((d) => { const paragraphs = [...d.paragraphs]; [paragraphs[index - 1], paragraphs[index]] = [paragraphs[index], paragraphs[index - 1]]; return { ...d, paragraphs }; })}>上移</button>
            <button className="text-button" disabled={index === doc.paragraphs.length - 1} onClick={() => edit((d) => { const paragraphs = [...d.paragraphs]; [paragraphs[index + 1], paragraphs[index]] = [paragraphs[index], paragraphs[index + 1]]; return { ...d, paragraphs }; })}>下移</button>
            <button className="text-button" onClick={() => edit((d) => ({ ...d, paragraphs: d.paragraphs.filter((p) => p.id !== paragraph.id) }))}>删除段落</button>
          </div></div>
          <div className="text-pair">{(['original_text', 'draft_text'] as const).map((field) => <label key={field}>{field === 'original_text' ? '普通话原稿' : '武汉话草稿'}<textarea aria-label={field === 'original_text' ? '普通话原稿' : '武汉话草稿'} aria-describedby={`${paragraph.id}-${field}-count`} rows={5} value={paragraph[field]} onChange={(e) => edit((d) => ({ ...d, paragraphs: d.paragraphs.map((p) => p.id === paragraph.id ? { ...p, [field]: e.target.value } : p) }))} /><small id={`${paragraph.id}-${field}-count`}>{length(paragraph[field])} / 5000 字</small></label>)}</div>
          <button className="text-button" onClick={() => edit((d) => ({ ...d, paragraphs: d.paragraphs.map((p) => p.id === paragraph.id ? { ...p, draft_text: p.original_text } : p) }))}>复制原稿到草稿</button>
          <label className="pronunciation-label">读音备注<textarea rows={2} value={paragraph.pronunciation_note} onChange={(e) => edit((d) => ({ ...d, paragraphs: d.paragraphs.map((p) => p.id === paragraph.id ? { ...p, pronunciation_note: e.target.value } : p) }))} /></label>
        </article>)}
        <button className="button secondary" disabled={doc.paragraphs.length >= 200} onClick={() => edit((d) => ({ ...d, paragraphs: [...d.paragraphs, { id: crypto.randomUUID(), original_text: '', draft_text: '', pronunciation_note: '' }] }))}>添加段落</button>
      </fieldset>
      {invalid && <p role="alert" className="invalid">{invalid}</p>}
      {doc.conversion_warnings.map((warning, index) => <p className="muted" key={index}>{warning}</p>)}
      <div className="text-confirm"><p>{dirty ? '有未保存修改，请先保存，再核对确认。' : current ? '文案已准备好；语音生成将在后续模块开放。' : doc.latest_confirmation ? '原确认已过时，请重新核对并确认当前文案。' : '尚未确认。请填写每段原稿与草稿，保存后核对。'}</p>
        <button className="button primary" disabled={busy || !dirty || !!invalid} onClick={() => void perform('save')}>保存文案</button>
        <label className="ack-label"><input type="checkbox" checked={ack} disabled={busy || dirty || !complete || !!invalid} onChange={(e) => setAck(e.target.checked)} />我已逐段核对原意、数字、专名及武汉话草稿</label>
        <button className="button secondary" disabled={busy || dirty || !complete || !!invalid || !ack} onClick={() => void perform('confirm')}>确认当前文案</button>
      </div>
      <p role="status" aria-label="文案操作状态" className="reference-notice">{notice}</p>
      <div className="text-history"><button className="text-button" onClick={() => void loadHistory()}>查看确认历史</button>
        {historyLoaded && history.length === 0 && <p className="muted">暂无确认记录。</p>}
        {history.map((item) => <button key={item.id} className="button secondary" onClick={() => void loadHistory(item.id)}>查看确认版本 {item.revision} · {new Date(item.created_at).toLocaleString('zh-CN')}</button>)}
        {snapshot && <section className="text-snapshot" aria-label="确认快照"><h3>确认版本 {snapshot.revision}（只读）</h3><p>{snapshot.source_language} → {snapshot.target_language} · {snapshot.register}</p><p>术语备注：{snapshot.terms || '无'}</p>{snapshot.paragraphs.map((p, index) => <article key={p.id}><h4>第 {index + 1} 段</h4><p>原稿：{p.original_text}</p><p>草稿：{p.draft_text}</p><p>读音备注：{p.pronunciation_note || '无'}</p></article>)}{snapshot.conversion_warnings.map((w, i) => <p key={i}>{w}</p>)}</section>}
      </div>
    </>}
  </section>;
}
