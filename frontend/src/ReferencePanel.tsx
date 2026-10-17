import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { AudioLines, Check, CircleAlert, Download, LoaderCircle, RefreshCw, Scissors, Upload } from 'lucide-react';
import { messageOf, request, type Project } from './api';

export type Clip = { id: string; reference_id: string; start_ms: number; end_ms: number; created_at: string };
export type ReferenceAudio = {
  id: string; project_id: string; filename: string; byte_size: number; sha256: string;
  duration_ms: number; created_at: string; clips: Clip[];
};
type Library = { references: ReferenceAudio[]; active_clip_id: string | null };
const seconds = (ms: number) => (ms / 1000).toFixed(3);
const sizeOf = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
const rangeOf = (clip: Clip) => `${seconds(clip.start_ms)}–${seconds(clip.end_ms)} 秒`;

function toMilliseconds(value: string): number | null {
  if (!/^\d+(?:\.\d{1,3})?$/.test(value)) return null;
  const number = Math.round(Number(value) * 1000);
  return Number.isSafeInteger(number) ? number : null;
}

function AudioPlayer({ url, label }: { url: string; label: string }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  return <div className="audio-player">
    <audio key={attempt} controls preload="metadata" src={url} aria-label={label} onError={() => setFailed(true)} />
    {failed && <div className="error-box" role="alert"><span>音频暂时无法播放，请检查本地服务和素材文件。</span>
      <button className="text-button" onClick={() => { setFailed(false); setAttempt((value) => value + 1); }}>重新加载音频</button></div>}
  </div>;
}

function ClipEditor({ reference, activeClipId, base, busy, saveClip, activate }: {
  reference: ReferenceAudio; activeClipId: string | null; base: string; busy: boolean;
  saveClip: (start: number, end: number) => Promise<Clip | null>; activate: (clipId: string) => Promise<void>;
}) {
  const initial = reference.clips.find((clip) => clip.id === activeClipId);
  const [start, setStart] = useState(seconds(initial?.start_ms ?? 0));
  const [end, setEnd] = useState(seconds(initial?.end_ms ?? reference.duration_ms));
  const [listeningId, setListeningId] = useState(initial?.id ?? reference.clips[0]?.id ?? '');
  const listening = reference.clips.find((clip) => clip.id === listeningId);
  const startMs = toMilliseconds(start);
  const endMs = toMilliseconds(end);
  const validation = startMs === null || endMs === null ? '请输入秒数，最多保留三位小数。' :
    startMs < 0 || endMs > reference.duration_ms ? '起止时间必须在音频范围内。' :
      endMs - startMs < 100 ? '结束时间须晚于开始时间，片段至少为 0.100 秒。' : null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (validation || startMs === null || endMs === null || busy) return;
    const saved = await saveClip(startMs, endMs);
    if (saved) setListeningId(saved.id);
  }

  return <div className="reference-editor">
    <div className="reference-title"><div><span className="eyebrow">ORIGINAL REFERENCE</span><h3>{reference.filename}</h3>
      <p>{seconds(reference.duration_ms)} 秒 · {sizeOf(reference.byte_size)} · 原件已保留</p></div>
      <a className="text-button" href={`${base}/original`} download><Download size={15} />下载原件</a></div>
    <label className="audio-label">完整试听</label>
    <AudioPlayer key={reference.id} url={`${base}/preview`} label="完整参考音频" />
    <p className="audio-hint">试听使用兼容副本，原始文件内容保持不变。</p>
    <form className="clip-form" onSubmit={submit}>
      <div className="subsection-title"><Scissors size={17} /><h4>选取参考片段</h4></div>
      <p className="audio-hint">输入起止秒数，保存后可试听。每次保存都会保留为新片段。</p>
      <fieldset disabled={busy}>
        <div className="time-fields"><div><label htmlFor="clip-start">开始（秒）</label>
          <input id="clip-start" type="number" min="0" max={reference.duration_ms / 1000} step="0.001" value={start}
            onChange={(event) => setStart(event.target.value)} aria-describedby="clip-hint" /></div>
        <span className="time-separator">—</span><div><label htmlFor="clip-end">结束（秒）</label>
          <input id="clip-end" type="number" min="0" max={reference.duration_ms / 1000} step="0.001" value={end}
            onChange={(event) => setEnd(event.target.value)} aria-describedby="clip-hint" /></div></div>
        <p id="clip-hint" className={`field-hint ${validation ? 'invalid' : ''}`}>
          {validation ?? `片段时长 ${seconds(endMs! - startMs!)} 秒`}</p>
        <button className="button primary" disabled={busy || !!validation} type="submit"><Scissors size={15} />保存为当前参考</button>
      </fieldset>
    </form>
    <div className="clip-history">
      <div className="subsection-title"><AudioLines size={17} /><h4>已保存片段</h4><span className="count">{reference.clips.length}</span></div>
      {reference.clips.length === 0 ? <p className="audio-hint">保存第一个片段后，可以在这里试听和切换。</p> : <>
        <label htmlFor="saved-clip" className="audio-label">选择试听片段</label>
        <select id="saved-clip" value={listeningId} disabled={busy} onChange={(event) => setListeningId(event.target.value)}>
          <option value="" disabled>选择一个已保存片段</option>
          {reference.clips.map((clip, index) => <option key={clip.id} value={clip.id}>
            片段 {reference.clips.length - index} · {rangeOf(clip)}{activeClipId === clip.id ? ' · 当前参考' : ''}
          </option>)}
        </select>
        {listening && <div className="saved-clip">
          <AudioPlayer key={listening.id} url={`${base}/clips/${listening.id}/audio`} label="已保存参考片段" />
          <div className="clip-actions"><span className={activeClipId === listening.id ? 'active-reference' : 'audio-hint'}>
            {activeClipId === listening.id ? <><Check size={14} />当前参考片段</> : '仅试听，尚未设为当前参考'}</span>
            {activeClipId !== listening.id && <button className="button secondary" disabled={busy} onClick={() => void activate(listening.id)}>设为当前参考</button>}
            <a className="text-button" href={`${base}/clips/${listening.id}/audio`} download={`reference-${listening.id}.wav`}><Download size={14} />下载片段</a>
          </div>
        </div>}
      </>}
    </div>
  </div>;
}

export default function ReferencePanel({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const [library, setLibrary] = useState<Library>({ references: [], active_clip_id: null });
  const [selectedId, setSelectedId] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const pending = useRef<AbortController | null>(null);
  const base = `/projects/${project.id}`;
  const selected = library.references.find((reference) => reference.id === selectedId);
  const active = library.references.find((reference) => reference.clips.some((clip) => clip.id === library.active_clip_id));

  const reload = useCallback(() => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true); setLoadError(''); setNotice('');
    request<Library>(`${base}/references`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        setLibrary(result);
        setSelectedId((previous) => result.references.some((reference) => reference.id === previous) ? previous :
          result.references.find((reference) => reference.clips.some((clip) => clip.id === result.active_clip_id))?.id ?? result.references[0]?.id ?? '');
      })
      .catch((failure: unknown) => { if (!controller.signal.aborted) setLoadError(messageOf(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
  }, [base]);

  useEffect(() => { reload(); return () => pending.current?.abort(); }, [reload]);

  async function mutate<T>(path: string, options: RequestInit, label: string, onSuccess: (result: T) => void): Promise<T | null> {
    if (busy || loading || loadError) return null;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setBusy(label); setError(''); setNotice('');
    try {
      const result = await request<T>(`${base}${path}`, { ...options, signal: controller.signal }, 120000);
      if (controller.signal.aborted) return null;
      onSuccess(result);
      onChanged();
      return result;
    } catch (failure) {
      if (!controller.signal.aborted) setError(messageOf(failure));
      return null;
    } finally {
      if (!controller.signal.aborted) setBusy('');
    }
  }

  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    if (file.size === 0 || file.size > 50 * 1024 * 1024) { setError('请选择非空音频文件，最大为 50 MiB。'); return; }
    if (!/\.(wav|mp3|m4a|flac|ogg)$/i.test(file.name)) { setError('支持 WAV、MP3、M4A、FLAC 和 OGG 文件。'); return; }
    await mutate<ReferenceAudio>(`/references?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
    }, '正在上传并检查音频…', (result) => {
      setLibrary((previous) => ({ ...previous, references: [result, ...previous.references] }));
      setSelectedId(result.id); setFile(null); if (input.current) input.current.value = '';
      setNotice('音频已保存在本机。选取并保存片段后即可设为当前参考。');
    });
  }

  return <section className="reference-panel" aria-label="参考音频管理" aria-busy={loading || !!busy}>
    <div className="reference-heading"><div><span className="eyebrow">01 / REFERENCE AUDIO</span><h2>参考声音<span className="heading-dot">.</span></h2>
      <p>为「{project.name}」挑选一段声音，保留原件，随时重新选取。</p></div>
      <button className="text-button" disabled={loading || !!busy} onClick={reload}><RefreshCw size={14} />刷新素材</button></div>
    <form className="upload-form" onSubmit={upload}>
      <div className="upload-symbol"><Upload size={22} /></div>
      <div className="upload-field"><label htmlFor="reference-file">导入参考音频</label><p>WAV、MP3、M4A、FLAC、OGG · 最大 50 MiB · 0.1–600 秒</p>
        <input ref={input} id="reference-file" type="file" accept=".wav,.mp3,.m4a,.flac,.ogg" disabled={loading || !!busy || !!loadError}
          onChange={(event) => { setFile(event.target.files?.[0] ?? null); setError(''); setNotice(''); }} /></div>
      <button className="button primary" disabled={!file || loading || !!busy || !!loadError} type="submit">
        {busy === '正在上传并检查音频…' ? <LoaderCircle size={15} className="spin" /> : <Upload size={15} />}上传音频</button>
    </form>
    <p className="reference-notice" role="status" aria-label="素材操作状态">{busy || notice}</p>
    {error && <div className="error-box" role="alert"><CircleAlert size={17} /><span>{error} 若操作已完成但未收到响应，可先刷新素材核对。</span></div>}
    {loading ? <p className="audio-loading"><LoaderCircle className="spin" size={17} />正在读取参考音频…</p> :
      loadError ? <div className="error-box" role="alert"><CircleAlert size={17} /><span>{loadError}</span><button className="text-button" onClick={reload}>重试读取素材</button></div> : <>
        <div className="current-reference"><Check size={15} /><span>{active ? `当前参考：${active.filename} · ${rangeOf(active.clips.find((clip) => clip.id === library.active_clip_id)!)}` : '尚未选择当前参考片段'}</span></div>
        {library.references.length === 0 ? <div className="reference-empty"><AudioLines size={32} strokeWidth={1.5} /><h3>把参考声音放进来</h3><p>上传后可以完整试听，再保存想使用的片段。</p></div> :
          <div className="reference-workspace"><nav className="reference-list" aria-label="参考素材列表">
            <h3>原始素材 <span className="count">{library.references.length}</span></h3>
            {library.references.map((reference) => <button key={reference.id} disabled={!!busy} aria-pressed={reference.id === selectedId}
              className={`reference-item ${reference.id === selectedId ? 'selected' : ''}`} onClick={() => { setSelectedId(reference.id); setError(''); setNotice(''); }}>
              <AudioLines size={18} /><span><strong>{reference.filename}</strong><small>{seconds(reference.duration_ms)} 秒 · {reference.clips.length} 个片段{reference.id === active?.id ? ' · 使用中' : ''}</small></span>
            </button>)}
          </nav>
            {selected && <ClipEditor key={selected.id} reference={selected} activeClipId={library.active_clip_id}
              base={`/api${base}/references/${selected.id}`} busy={!!busy}
              saveClip={(start_ms, end_ms) => mutate<Clip>(`/references/${selected.id}/clips`, {
                method: 'POST', body: JSON.stringify({ start_ms, end_ms }),
              }, '正在保存参考片段…', (clip) => {
                setLibrary((previous) => ({ active_clip_id: clip.id, references: previous.references.map((reference) =>
                  reference.id === selected.id ? { ...reference, clips: [clip, ...reference.clips] } : reference) }));
                setNotice('新片段已保存并设为当前参考，原件和旧片段均已保留。');
              })}
              activate={async (clipId) => { await mutate<{ active_clip_id: string }>('/active-clip', {
                method: 'PUT', body: JSON.stringify({ clip_id: clipId }),
              }, '正在切换当前参考…', (result) => {
                setLibrary((previous) => ({ ...previous, active_clip_id: result.active_clip_id }));
                setNotice('当前参考片段已切换。');
              }); }} />}
          </div>}
      </>}
  </section>;
}
