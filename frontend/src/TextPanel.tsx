import { useEffect, useRef, useState } from 'react';
import { messageOf, request } from './api';
import { pendingDrafts } from './textDraftCache';
import TaskPanel from './TaskPanel';
import TextImport from './TextImport';
import { advancedPreference, saveAdvanced } from './uiPreferences';

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

export default function TextPanel({ projectId, onChanged, referenceRefresh }: { projectId: string; onChanged: () => void; referenceRefresh: number }) {
  const [doc, setDoc] = useState<TextDocument | null>(() => pendingDrafts.get(projectId)?.doc ?? null);
  const [saved, setSaved] = useState(() => pendingDrafts.get(projectId)?.saved ?? '');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [undo, setUndo] = useState<{ paragraph: Paragraph; index: number; kind: 'copy' | 'delete'; copied?: string } | null>(null);
  const [ack, setAck] = useState(false);
  const [advanced, setAdvanced] = useState(advancedPreference);
  const [editingId, setEditingId] = useState('');
  const preparing = useRef(false);
  const dragged = useRef<string | null>(null);
  const [history, setHistory] = useState<Pick<Confirmation, 'id' | 'revision' | 'created_at'>[]>([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [snapshotError, setSnapshotError] = useState('');
  const [snapshot, setSnapshot] = useState<Confirmation | null>(null);
  const controller = useRef<AbortController | null>(null);
  const operation = useRef(0);
  const historyOpened = useRef(false);
  const historyListOperation = useRef(0);
  const snapshotOperation = useRef(0);
  const base = `/projects/${projectId}/text`;
  const dirty = !!doc && JSON.stringify(contentOf(doc)) !== saved;
  const invalid = doc ? validation(doc) : null;
  const complete = !!doc?.paragraphs.length && doc.paragraphs.every((p) => p.original_text.trim() && p.draft_text.trim());
  const current = !!doc?.confirmation_current && !dirty;
  const reservedUndoSlot = undo?.kind === 'delete' && !doc?.paragraphs.some(p => p.id === undo.paragraph.id) ? 1 : 0;
  const additionBlocked = !!doc && doc.paragraphs.length + reservedUndoSlot >= 200;

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
    return () => { abort.abort(); operation.current += 1; historyListOperation.current += 1; snapshotOperation.current += 1; };
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
      setDoc(value); setSaved(JSON.stringify(contentOf(value))); setAck(false); setUndo(null);
      setNotice(kind === 'save' ? '文案已保存，请核对后人工确认。' : kind === 'confirm' ? '文案已确认。' : '已载入服务器版本。');
      if (kind !== 'reload') onChanged();
      // The confirmation is already committed; this independent read must not undo its success.
      if (kind === 'confirm' && historyOpened.current) void refreshHistory(true);
    } catch (cause) {
      if (!abort.signal.aborted && version === operation.current) setError(`${messageOf(cause)} 本地输入已保留；如有版本冲突，可重新载入服务器版本。`);
    } finally { if (!abort.signal.aborted && version === operation.current) setBusy(false); }
  }
  async function prepareDocument(): Promise<TextDocument> {
    const abort = controller.current;
    if (!doc || !abort || abort.signal.aborted || busy || preparing.current) throw new Error('请等待文案读取或操作完成。');
    const prepared = { ...doc, paragraphs: doc.paragraphs.map(p => ({ ...p, original_text: p.original_text.trim() ? p.original_text : p.draft_text })) };
    const problem = validation(prepared);
    if (problem || !prepared.paragraphs.length || prepared.paragraphs.some(p => !p.draft_text.trim())) {
      setError(problem || '请填写每一段要朗读的文案。');
      throw new Error(problem || '请填写每一段要朗读的文案。');
    }
    preparing.current = true; setBusy(true); setError(''); setNotice('正在保存本次文案…');
    const version = ++operation.current;
    try {
      let value = prepared;
      if (JSON.stringify(contentOf(prepared)) !== saved || !prepared.revision) {
        value = await request<TextDocument>(base, { method: 'PUT', signal: abort.signal,
          body: JSON.stringify({ ...contentOf(prepared), expected_revision: prepared.revision }) });
        if (abort.signal.aborted || version !== operation.current) throw new Error('项目已切换，本次生成已停止。');
        setDoc(value); setSaved(JSON.stringify(contentOf(value))); setUndo(null); onChanged();
      }
      if (!value.confirmation_current || !value.latest_confirmation) {
        setNotice('正在固定本次生成文案…');
        value = await request<TextDocument>(base + '/confirm', { method: 'POST', signal: abort.signal,
          body: JSON.stringify({ expected_revision: value.revision, acknowledged: true }) });
      }
      if (abort.signal.aborted || version !== operation.current) throw new Error('项目已切换，本次生成已停止。');
      setDoc(value); setSaved(JSON.stringify(contentOf(value))); setAck(false); setUndo(null);
      setNotice('本次文案已保存并确认。'); onChanged();
      if (historyOpened.current) void refreshHistory(true);
      return value;
    } catch (cause) {
      if (!abort.signal.aborted && version === operation.current) { setNotice(''); setError(messageOf(cause) + ' 输入已保留；如有版本冲突，请重新载入后核对。'); }
      throw cause;
    } finally {
      preparing.current = false;
      if (!abort.signal.aborted && version === operation.current) setBusy(false);
    }
  }
  async function refreshHistory(afterConfirmation = false) {
    const abort = controller.current;
    if (!abort) return;
    historyOpened.current = true;
    const version = ++historyListOperation.current;
    setHistoryError('');
    try {
      const value = await request<{ confirmations: typeof history }>(`${base}/confirmations`, { signal: abort.signal });
      if (!abort.signal.aborted && version === historyListOperation.current) {
        setHistory(value.confirmations); setHistoryLoaded(true);
      }
    } catch (cause) {
      if (!abort.signal.aborted && version === historyListOperation.current) {
        setHistoryError(`${afterConfirmation ? '文案已确认，但确认历史刷新失败。' : '确认历史读取失败。'} ${messageOf(cause)} 请点击“查看确认历史”重试。`);
      }
    }
  }
  async function loadSnapshot(id: string) {
    const abort = controller.current;
    if (!abort) return;
    const version = ++snapshotOperation.current;
    setSnapshotError('');
    try {
      const value = await request<Confirmation>(`${base}/confirmations/${id}`, { signal: abort.signal });
      if (!abort.signal.aborted && version === snapshotOperation.current) setSnapshot(value);
    } catch (cause) {
      if (!abort.signal.aborted && version === snapshotOperation.current) setSnapshotError(`确认快照读取失败。 ${messageOf(cause)} 请重新选择历史版本。`);
    }
  }
  return <><section className="reference-panel text-panel" aria-label="文案工作台" aria-busy={busy}>
    <div className="reference-heading"><div><span className="eyebrow">TEXT WORKSPACE</span><h2>写下要朗读的文案</h2><p>填写后点击下方生成。点击生成即确认使用当前文案，系统会自动保存。</p></div></div>
    {error && <p className="error-box" role="alert">{error}</p>}
    <div className="text-actions"><button className="text-button" disabled={busy} onClick={() => void perform('reload')}>重新载入服务器版本</button></div>
    {!doc ? <p className="muted">{error ? '文案未能读取，请点击“重新载入服务器版本”重试。' : '正在读取文案…'}</p> : <>
      <label className="audio-label">要朗读的文案
        <textarea aria-label="要朗读的文案" rows={7} style={{ width: '100%' }} disabled={busy} value={(doc.paragraphs.find(p => p.id === editingId) ?? doc.paragraphs[0])?.draft_text ?? ''}
          placeholder="在这里输入需要读出的文字…" onChange={event => {
            const text = event.target.value;
            edit(d => {
              const selected = d.paragraphs.find(p => p.id === editingId) ?? d.paragraphs[0];
              if (!selected) return { ...d, paragraphs: [{ id: crypto.randomUUID(), original_text: '', draft_text: text, pronunciation_note: '' }] };
              return { ...d, paragraphs: d.paragraphs.map(p => p.id === selected.id ? { ...p, draft_text: text } : p) };
            });
          }} />
      </label>
      <p className="audio-hint">每段最多5000字；不自动改写或翻译。多段项目生成时包含全部段落。</p>
      <TextImport key={`${projectId}:${(doc.paragraphs.find(p => p.id === editingId) ?? doc.paragraphs[0])?.id ?? 'new'}`}
        baseline={JSON.stringify(contentOf(doc))} disabled={busy} onApply={text => {
          if (!text.trim()) return '导入文字不能为空。';
          if ([...text].some(char => /\p{Cc}/u.test(char) && char !== '\n' && char !== '\t')) return '文字不能包含换行和制表符以外的控制字符。';
          const selected = doc.paragraphs.find(p => p.id === editingId) ?? doc.paragraphs[0];
          const updated = { ...doc, paragraphs: selected ? doc.paragraphs.map(p => p.id === selected.id ? { ...p, draft_text: text } : p) : [{ id: crypto.randomUUID(), original_text: '', draft_text: text, pronunciation_note: '' }] };
          const problem = validation(updated); if (problem) return problem;
          if (selected?.draft_text && selected.draft_text !== text && !window.confirm('这会替换当前段已有的朗读文案，其他段和原稿保留。是否继续？')) return '已取消替换，原文案保留。';
          edit(() => updated); setNotice('已导入当前段，尚未保存；请核对后生成。'); return null;
        }} />
      {doc.paragraphs.length > 1 && <label>编辑哪一段<select disabled={busy} value={(doc.paragraphs.find(p => p.id === editingId) ?? doc.paragraphs[0]).id} onChange={e => setEditingId(e.target.value)}>
        {doc.paragraphs.map((p, i) => <option key={p.id} value={p.id}>第 {i + 1} 段 · {p.draft_text.slice(0, 24)}</option>)}
      </select><span>已保留原有 {doc.paragraphs.length} 段及顺序。</span></label>}
      {invalid && <p role="alert" className="invalid">{invalid}</p>}
      <details open={advanced} onToggle={e => { setAdvanced(e.currentTarget.open); saveAdvanced(e.currentTarget.open); }}>
      <summary>高级分段编辑与历史</summary>
      <div className="text-language">源语言：普通话（zh-CN） <span>目标：武汉话（wuhan）</span><span>已保存版本：{doc.revision}</span></div>
      <fieldset disabled={busy} className="text-fields">
        <p className="audio-hint">分段排序用于拼接。旧原稿和备注保留；备注不控制发音。可拖动段落，或用上移、下移调整顺序。</p>
        <p className="audio-hint">请手工填写或粘贴需要朗读的草稿，核对后人工确认。复制原稿仅复制文字，不会自动改写为武汉话。</p>
        {doc.paragraphs.map((paragraph, index) => <article className="text-paragraph" key={paragraph.id} data-paragraph-id={paragraph.id} aria-label={`第 ${index + 1} 段`} draggable={!busy}
          onDragStart={e => { dragged.current = paragraph.id; e.dataTransfer.setData('application/x-voice-paragraph', paragraph.id); }} onDragEnd={() => { dragged.current = null; }} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); const id = dragged.current; dragged.current = null; if (!id || e.dataTransfer.getData('application/x-voice-paragraph') !== id || id === paragraph.id) return; edit(d => { const from = d.paragraphs.findIndex(p => p.id === id); if (from < 0) return d; const to = d.paragraphs.findIndex(p => p.id === paragraph.id); const paragraphs = [...d.paragraphs]; const [moved] = paragraphs.splice(from, 1); paragraphs.splice(to, 0, moved); return { ...d, paragraphs }; }); }}>
          <div className="text-paragraph-heading"><h3>第 {index + 1} 段</h3><div className="text-actions">
            <button className="text-button" disabled={index === 0} onClick={() => edit((d) => { const paragraphs = [...d.paragraphs]; [paragraphs[index - 1], paragraphs[index]] = [paragraphs[index], paragraphs[index - 1]]; return { ...d, paragraphs }; })}>上移</button>
            <button className="text-button" disabled={index === doc.paragraphs.length - 1} onClick={() => edit((d) => { const paragraphs = [...d.paragraphs]; [paragraphs[index + 1], paragraphs[index]] = [paragraphs[index], paragraphs[index + 1]]; return { ...d, paragraphs }; })}>下移</button>
            <button className="text-button" onClick={() => { setUndo({ paragraph, index, kind: 'delete' }); edit((d) => ({ ...d, paragraphs: d.paragraphs.filter((p) => p.id !== paragraph.id) })); }}>删除段落</button>
          </div></div>
          <div className="text-pair">{(['original_text', 'draft_text'] as const).map((field) => <label key={field}>{field === 'original_text' ? '普通话原稿' : '武汉话草稿'}<textarea aria-label={field === 'original_text' ? '普通话原稿' : '武汉话草稿'} aria-describedby={`${paragraph.id}-${field}-count`} rows={5} value={paragraph[field]} onChange={(e) => edit((d) => ({ ...d, paragraphs: d.paragraphs.map((p) => p.id === paragraph.id ? { ...p, [field]: e.target.value } : p) }))} /><small id={`${paragraph.id}-${field}-count`}>{length(paragraph[field])} / 5000 字</small></label>)}</div>
          <button className="text-button" onClick={() => {
            if (paragraph.draft_text && paragraph.draft_text !== paragraph.original_text && !window.confirm('这会覆盖本段已有草稿，是否继续？取消会保留人工修改。')) return;
            setUndo({ paragraph, index, kind: 'copy', copied: paragraph.original_text });
            edit((d) => ({ ...d, paragraphs: d.paragraphs.map((p) => p.id === paragraph.id ? { ...p, draft_text: p.original_text } : p) }));
          }}>复制原稿到草稿</button>
          {!!paragraph.pronunciation_note && <p aria-label="已存读音备注">已存读音备注（仅记录）：{paragraph.pronunciation_note}</p>}
        </article>)}
        {undo && <div className="field-hint">可撤销最近一次复制或删除（仅当前项目页面，保存或重载后清除）。
          <button className="text-button" onClick={() => {
            const previous = undo;
            const existing = doc.paragraphs.find(p => p.id === previous.paragraph.id);
            if (!existing && doc.paragraphs.length >= 200) { setError('最多保存 200 段，暂不能恢复；待撤销内容已保留。'); return; }
            if (previous.kind === 'copy' && existing?.draft_text !== previous.copied && !window.confirm('复制后草稿又有修改，撤销会恢复复制前的草稿，是否继续？')) return;
            edit(d => { const paragraphs = [...d.paragraphs]; const at = paragraphs.findIndex(p => p.id === previous.paragraph.id);
              if (at >= 0) paragraphs[at] = previous.kind === 'copy' ? { ...paragraphs[at], draft_text: previous.paragraph.draft_text } : previous.paragraph;
              else paragraphs.splice(Math.min(previous.index, paragraphs.length), 0, previous.paragraph);
              return { ...d, paragraphs }; });
            setUndo(null);
          }}>撤销最近一次复制或删除</button></div>}
        {!!reservedUndoSlot && additionBlocked && <p className="field-hint">已为待撤销的段落保留一个位置（最多 200 段）。请先撤销，或保存当前删除后再添加。</p>}
        <button className="button secondary" disabled={additionBlocked} onClick={() => {
          if (additionBlocked) return;
          edit((d) => ({ ...d, paragraphs: [...d.paragraphs, { id: crypto.randomUUID(), original_text: '', draft_text: '', pronunciation_note: '' }] }));
        }}>添加段落</button>
      </fieldset>
      {invalid && <p role="alert" className="invalid">{invalid}</p>}
      {doc.conversion_warnings.map((warning, index) => <p className="muted" key={index}>{warning}</p>)}
      <div className="text-confirm"><p>{dirty ? '有未保存修改，请先保存，再核对确认。' : current ? '文案已准备好，可在下方选择生成组件并核对提交条件。' : doc.latest_confirmation ? '原确认已过时，请重新核对并确认当前文案。' : '尚未确认。请填写每段原稿与草稿，保存后核对。'}</p>
        <button className="button primary" disabled={busy || !dirty || !!invalid} onClick={() => void perform('save')}>保存文案</button>
        <label className="ack-label"><input type="checkbox" checked={ack} disabled={busy || dirty || !complete || !!invalid} onChange={(e) => setAck(e.target.checked)} />我已逐段核对原意、数字、专名及武汉话草稿</label>
        <button className="button secondary" disabled={busy || dirty || !complete || !!invalid || !ack} onClick={() => void perform('confirm')}>确认当前文案</button>
      </div>

      <div className="text-history"><button className="text-button" onClick={() => void refreshHistory()}>查看确认历史</button>
        {(historyError || snapshotError) && <p className="error-box" role="alert" aria-label="确认历史状态">{[historyError, snapshotError].filter(Boolean).join(' ')}</p>}
        {historyLoaded && history.length === 0 && <p className="muted">暂无确认记录。</p>}
        {history.map((item) => <button key={item.id} className="button secondary" onClick={() => void loadSnapshot(item.id)}>查看确认版本 {item.revision} · {new Date(item.created_at).toLocaleString('zh-CN')}</button>)}
        {snapshot && <section className="text-snapshot" aria-label="确认快照"><h3>确认版本 {snapshot.revision}（只读）</h3><p>{snapshot.source_language} → {snapshot.target_language} · {snapshot.register}</p><p>术语备注：{snapshot.terms || '无'}</p>{snapshot.paragraphs.map((p, index) => <article key={p.id}><h4>第 {index + 1} 段</h4><p>原稿：{p.original_text}</p><p>草稿：{p.draft_text}</p><p>读音备注：{p.pronunciation_note || '无'}</p></article>)}{snapshot.conversion_warnings.map((w, i) => <p key={i}>{w}</p>)}</section>}
      </div>
      </details>
      <button className="text-button" disabled={busy || !!invalid || !doc.paragraphs.length} onClick={() => { void prepareDocument().catch(() => {}); }}>保存并确认文案与顺序</button>
      <p role="status" aria-label="文案操作状态" className="reference-notice">{notice}</p>
    </>}
  </section><TaskPanel projectId={projectId} doc={doc} dirty={dirty} textBusy={busy} textError={!!error} referenceRefresh={referenceRefresh} onChanged={onChanged} prepareDocument={prepareDocument} /></>;
}
