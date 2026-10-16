import { useEffect, useRef, useState } from 'react';
import { messageOf, request } from './api';
import { transcriptDraftKey, transcriptDrafts } from './transcriptDraftCache';

export type ReferenceTranscript = {
  clip_id: string;
  clip_sha256: string;
  revision: number;
  text: string;
  updated_at: string | null;
};

export type TranscriptReadiness = {
  clipId: string;
  value: ReferenceTranscript | null;
  ready: boolean;
};

// The parent keys this editor by the saved clip, so a late response cannot become
// the transcript of another clip. Saving text never changes the selected audio.
export default function ReferenceTranscriptEditor({ projectId, referenceId, clipId, onReady, onChanged }: {
  projectId: string;
  referenceId: string;
  clipId: string;
  onReady: (value: TranscriptReadiness) => void;
  onChanged: () => void;
}) {
  const base = `/projects/${projectId}/references/${referenceId}/clips/${clipId}/transcript`;
  const cacheKey = transcriptDraftKey(projectId, clipId);
  const [saved, setSaved] = useState<ReferenceTranscript | null>(() => transcriptDrafts.get(cacheKey)?.saved ?? null);
  const [text, setText] = useState(() => transcriptDrafts.get(cacheKey)?.text ?? '');
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const lifetime = useRef<AbortController | null>(null);
  const operation = useRef(0);
  const busyRef = useRef(false);
  const dirty = saved !== null && text !== saved.text;

  useEffect(() => {
    const abort = new AbortController();
    lifetime.current = abort;
    return () => { abort.abort(); operation.current += 1; };
  }, [clipId]);

  useEffect(() => {
    const abort = new AbortController();
    const version = ++operation.current;
    request<ReferenceTranscript>(base, { signal: abort.signal }).then((value) => {
      if (abort.signal.aborted || version !== operation.current) return;
      const pending = refresh === 0 ? transcriptDrafts.get(cacheKey) : undefined;
      setSaved(pending?.saved ?? value); setText(pending?.text ?? value.text); setError('');
      if (!pending) transcriptDrafts.delete(cacheKey);
      setNotice(pending ? '已恢复本窗口未保存的片段原话，请保存；版本冲突时可明确重新载入。' : '已读取该片段的原话。');
      setReading(false);
    }).catch((cause) => {
      if (!abort.signal.aborted && version === operation.current) { setError(messageOf(cause)); setReading(false); }
    });
    return () => { abort.abort(); };
  }, [base, cacheKey, refresh]);

  useEffect(() => {
    onReady({ clipId, value: saved, ready: saved !== null && saved.revision > 0 && !!saved.text.trim() && !dirty && !busy && !reading && !error });
  }, [clipId, saved, dirty, busy, reading, error, onReady]);

  async function save() {
    const abort = lifetime.current;
    if (!saved || !dirty || reading || busyRef.current || !abort || abort.signal.aborted) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice('');
    onReady({ clipId, value: saved, ready: false });
    const version = ++operation.current;
    try {
      const value = await request<ReferenceTranscript>(base, { method: 'PUT', signal: abort.signal,
        body: JSON.stringify({ expected_revision: saved.revision, expected_clip_sha256: saved.clip_sha256, text }) });
      if (abort.signal.aborted || version !== operation.current) return;
      transcriptDrafts.delete(cacheKey);
      setSaved(value); setText(value.text); setNotice(`片段原话已保存 · 版本 ${value.revision}`);
      onChanged();
    } catch (cause) {
      if (!abort.signal.aborted && version === operation.current) setError(`${messageOf(cause)} 输入已保留。重新载入前请复制需要保留的文字。`);
    } finally {
      busyRef.current = false;
      if (!abort.signal.aborted) setBusy(false);
    }
  }

  function reload() {
    if (reading || busyRef.current || (dirty && !window.confirm('重新载入会丢弃尚未保存的片段原话，是否继续？'))) return;
    // Keep the visible text until the explicit reload succeeds. A failure never
    // turns a locally edited transcript into a confirmed, usable version.
    onReady({ clipId, value: saved, ready: false });
    setReading(true); setError(''); setRefresh((value) => value + 1);
  }

  return <section className="transcript-editor" aria-label="参考片段原话">
    <h3>该片段实际说出的原话</h3>
    <p className="audio-hint">逐字填写当前起止范围内的录音内容。切换组件、片段或项目会保留本窗口未保存原话；刷新或关闭页面可能丢失，请及时保存。</p>
    <label className="audio-label" htmlFor={`transcript-${clipId}`}>参考片段准确原话</label>
    <textarea id={`transcript-${clipId}`} value={text} disabled={!saved || busy || reading} rows={3}
      onChange={(event) => {
        const value = event.target.value;
        if (saved && value !== saved.text) transcriptDrafts.set(cacheKey, { saved, text: value });
        else transcriptDrafts.delete(cacheKey);
        setText(value); setNotice(''); onReady({ clipId, value: saved, ready: false });
      }} />
    {dirty && <p className="field-hint">片段原话尚未保存，暂不能提交真实生成任务。</p>}
    {reading && <p className="muted">正在读取片段原话，请稍候再编辑…</p>}
    <div className="task-actions"><button className="button secondary" disabled={!saved || busy || reading || !dirty}
      onClick={() => void save()}>保存片段原话</button>
      <button className="text-button" disabled={busy || reading} onClick={reload}>重新载入片段原话</button></div>
    {error && <p role="alert" className="error-box">{error}</p>}
    <p role="status" aria-label="片段原话状态">{notice}</p>
  </section>;
}
