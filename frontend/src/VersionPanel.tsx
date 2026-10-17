import { useEffect, useRef, useState } from 'react';
import { messageOf, request } from './api';
import ExportPanel from './ExportPanel';

export type AudioVersion = {
  id: string; paragraph_id: string; task_id: string; number: number; ordinal: number;
  created_at: string; duration_ms: number; is_simulated: boolean; legacy: boolean;
  mode: 'preview' | 'full'; available: boolean; comparison: 'matching' | 'stale' | 'unknown';
  comparison_complete: boolean; stale_reasons: string[];
  adapter: { id: string; version: string; model_id?: string; model_revision?: string };
};
export type VersionParagraph = { id: string; draft_text: string; versions: AudioVersion[];
  selection: { version_id: string; revision: number } | null };
export type VersionCatalogue = { comparison_token: string; adapter_id: string; document_revision: number;
  confirmation_current: boolean; readiness: string[]; paragraphs: VersionParagraph[];
  archived_versions: AudioVersion[]; legacy_full_count: number };
const stateLabel = (v: AudioVersion) => v.comparison === 'matching' ? '匹配已保存稿' : v.comparison === 'stale' ? '已过时' : '依赖无法核定';
const audioUrl = (project: string, version: string) => `/api/projects/${project}/paragraph-versions/${version}/audio`;

function VersionAudio({ projectId, version }: { projectId: string; version: AudioVersion }) {
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  return <div><audio key={retry} controls preload="none" src={audioUrl(projectId, version.id)}
    aria-label={`试听版本 ${version.number}`} onError={() => setError(true)} />
    {error && <p role="alert" className="error-box">段落音频播放失败，请核对文件或服务。
      <button className="text-button" onClick={() => { setError(false); setRetry(retry + 1); }}>重载本段音频</button></p>}</div>;
}

function NeighborPlayer({ projectId, paragraphs, candidate }: {
  projectId: string; paragraphs: VersionParagraph[]; candidate: AudioVersion;
}) {
  const at = paragraphs.findIndex((p) => p.id === candidate.paragraph_id);
  const entries = [at - 1, at, at + 1].filter((index) => index >= 0 && index < paragraphs.length).map((index) => {
    const paragraph = paragraphs[index];
    return { label: `${index === at ? '当前候选' : index < at ? '前段选用' : '后段选用'} · 第 ${index + 1} 段`,
      version: index === at ? candidate : paragraph.versions.find((v) => v.id === paragraph.selection?.version_id) };
  });
  const [index, setIndex] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [message, setMessage] = useState('');
  const player = useRef<HTMLAudioElement>(null);
  const allAvailable = entries.every((entry) => entry.version?.available);
  const source = entries[index]?.version;
  useEffect(() => {
    if (!playing || !source) return;
    let disposed = false;
    const audio = player.current;
    audio?.play().catch(() => { if (!disposed) { setPlaying(false); setMessage('连续试听播放失败，请核对音频后重新开始。'); } });
    return () => { disposed = true; audio?.pause(); };
  }, [playing, index, source, attempt]);
  return <section className="neighbor-player" aria-label="前后段试听">
    <h4>按当前已保存顺序试听</h4>
    <ol>{entries.map((entry) => <li key={entry.label}>{entry.label}：{entry.version ?
      `版本 ${entry.version.number} · ${stateLabel(entry.version)} · ${entry.version.is_simulated ? '模拟测试音' : '真实生成'}${entry.version.available ? '' : ' · 文件不可用'}` : '尚未选用音频'}</li>)}</ol>
    <p className="audio-hint">依次播放前段选用、当前候选、后段选用，不保存拼接文件。输入匹配不代表声音衔接自然。</p>
    {!allAvailable && <p className="field-hint">邻段缺少可用选用，请先补齐；当前候选仍可单独试听。</p>}
    <button className="button secondary" disabled={!allAvailable || playing} onClick={() => { setIndex(0); setAttempt((value) => value + 1); setMessage(''); setPlaying(true); }}>开始连续试听</button>
    <button className="text-button" disabled={!playing} onClick={() => { setPlaying(false); setMessage('已停止试听'); }}>停止试听</button>
    {source && <audio key={`${attempt}:${index}`} ref={player} src={audioUrl(projectId, source.id)} preload="none" aria-label="邻段连续播放器"
      onEnded={() => { if (index + 1 < entries.length) setIndex(index + 1); else { setPlaying(false); setMessage('连续试听完成'); } }}
      onError={() => { setPlaying(false); setMessage('连续试听播放失败，请核对音频后重新开始。'); }} />}
    <p role="status" aria-label="连续试听状态">{playing ? `正在播放：${entries[index].label}` : message}</p>
  </section>;
}

export default function VersionPanel({ projectId, adapterId, refreshStamp, dirty, blocked, onGenerate, onChanged }: {
  projectId: string; adapterId: string; refreshStamp: string; dirty: boolean; blocked: string;
  onGenerate: (paragraphId: string) => void; onChanged: () => void;
}) {
  const stamp = `${projectId}:${adapterId}:${refreshStamp}`;
  const [refresh, setRefresh] = useState(0);
  const [loaded, setLoaded] = useState<{ value: VersionCatalogue; stamp: string; refresh: number } | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [candidateId, setCandidateId] = useState('');
  const controller = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const base = `/projects/${projectId}`;
  const ready = !!loaded && loaded.stamp === stamp && loaded.refresh === refresh && !error;
  const catalog = loaded?.stamp === stamp ? loaded.value : null;
  const candidates = catalog?.paragraphs.flatMap((p) => p.versions) ?? [];
  const candidate = candidates.find((v) => v.id === candidateId);
  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    const operation = ++sequence.current;
    void request<VersionCatalogue>(`${base}/paragraph-versions?adapter_id=${encodeURIComponent(adapterId)}`, { signal: abort.signal })
      .then((value) => {
        if (abort.signal.aborted || operation !== sequence.current) return;
        setLoaded({ value, stamp, refresh }); setError(''); setBusy(false); busyRef.current = false;
      }).catch((cause) => {
        if (abort.signal.aborted || operation !== sequence.current) return;
        setError(`版本读取失败：${messageOf(cause)} 请刷新版本后核对。`); setBusy(false); busyRef.current = false;
      });
    return () => { abort.abort(); sequence.current += 1; };
  }, [base, adapterId, stamp, refresh]);

  async function choose(paragraph: VersionParagraph, version: AudioVersion) {
    const abort = controller.current;
    if (!ready || !catalog || !abort || abort.signal.aborted || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice(''); sequence.current += 1;
    try {
      await request(`${base}/paragraphs/${paragraph.id}/selection`, { method: 'PUT', signal: abort.signal,
        body: JSON.stringify({ version_id: version.id, expected_revision: paragraph.selection?.revision ?? 0,
          adapter_id: adapterId, comparison_token: catalog.comparison_token, acknowledge_mismatch: version.comparison !== 'matching' }) });
      if (abort.signal.aborted) return;
      setNotice(`已保存第 ${catalog.paragraphs.indexOf(paragraph) + 1} 段的版本 ${version.number} 选用。${version.comparison === 'matching' ? '' : '原有过时或无法核定提示继续保留。'}`);
      onChanged(); setRefresh((value) => value + 1);
    } catch (cause) {
      if (!abort.signal.aborted) setError(`选用结果需要核对：${messageOf(cause)} 当前显示保留，请刷新版本确认实际选用后再操作。`);
    } finally { if (!abort.signal.aborted) { setBusy(false); busyRef.current = false; } }
  }

  return <section className="version-panel" aria-label="段落版本与局部返修">
    <div className="subsection-title"><h3>段落版本与局部返修</h3>
      <button className="text-button" disabled={busy} onClick={() => { setCandidateId(''); setRefresh((value) => value + 1); }}>刷新版本</button></div>
    <p className="audio-hint">以当前选择的生成组件与服务器已保存稿比较。选用仅表示保留此版本用于后续，不代表音色或武汉话已验收；新生成不会自动替换选用。</p>
    {dirty && <p className="field-hint">有未保存文案：下面的比较和顺序不包含本地修改，请先保存再核对。</p>}
    {error && <p role="alert" className="error-box">{error}</p>}
    {!ready && !error && <p className="muted">正在刷新版本及输入关系…</p>}
    <p role="status" aria-label="版本操作状态">{notice}</p>
    {catalog?.readiness.map((reason) => <p key={reason} className="field-hint">{reason}</p>)}
    {catalog && catalog.legacy_full_count > 0 && <p className="audio-hint">有 {catalog.legacy_full_count} 个旧全文任务未保存段边界，请在任务记录中试听整篇。</p>}
    {catalog?.paragraphs.map((paragraph, index) => <section className="paragraph-versions" key={paragraph.id} aria-label={`第 ${index + 1} 段版本`} data-version-paragraph={paragraph.id}>
      <div className="subsection-title"><h4>第 {index + 1} 段</h4><button className="button secondary" disabled={!ready || busy || !!blocked}
        onClick={() => onGenerate(paragraph.id)}>按当前确认稿重生成本段</button></div>
      <p className="version-draft">{paragraph.draft_text}</p>
      {blocked && <p className="audio-hint">{blocked}</p>}
      <p>{paragraph.selection ? `已选用版本 ${paragraph.versions.find((v) => v.id === paragraph.selection?.version_id)?.number ?? '（记录待核对）'}` : '尚未选用版本'}</p>
      {!paragraph.versions.length && <p className="muted">尚无本段音频。</p>}
      {paragraph.versions.map((version) => <article className="version-card" key={version.id} data-version-id={version.id} aria-label={`版本 ${version.number}`}>
        <div className="task-card-heading"><strong>版本 {version.number}{paragraph.selection?.version_id === version.id ? ' · 已选用' : ''}</strong>
          <span className={`version-state version-${version.comparison}`}>{stateLabel(version)}</span></div>
        <p className="audio-hint">{version.is_simulated ? '模拟测试音 · 不朗读文案' : '真实模型生成 · 效果待试听'} · {new Date(version.created_at).toLocaleString('zh-CN')} · {(version.duration_ms / 1000).toFixed(3)} 秒</p>
        <p className="audio-hint">来源：{version.legacy ? '旧短段映射' : version.mode === 'full' ? `全文任务第 ${version.ordinal} 段` : '独立短段任务'} · {version.adapter.id} / {version.adapter.version}</p>
        {version.stale_reasons.length > 0 && <p className="field-hint">{version.stale_reasons.join('；')}</p>}
        {!version.comparison_complete && <p className="field-hint">部分依赖当前无法核定，请刷新或检查模型与参考。</p>}
        {version.available ? <VersionAudio projectId={projectId} version={version} /> : <p className="error-box">音频文件不可用，历史和原选用仍保留。</p>}
        <div className="task-actions"><button className="button secondary" disabled={!ready || busy || !version.available}
          onClick={() => void choose(paragraph, version)}>{version.comparison === 'stale' ? '仍选用此过时版本' : version.comparison === 'unknown' ? '仍选用此无法核定版本' : '选用此版本'}</button>
          <button className="text-button" disabled={!ready || !version.available} onClick={() => setCandidateId(version.id)}>前后段试听</button></div>
      </article>)}
    </section>)}
    {catalog && <ExportPanel key={projectId} projectId={projectId} catalog={catalog} disabled={!ready || busy || dirty} />}
    {candidate && catalog && ready && <NeighborPlayer key={`${catalog.comparison_token}:${catalog.paragraphs.map((p) => p.selection?.revision ?? 0).join(',')}:${candidate.id}`}
      projectId={projectId} paragraphs={catalog.paragraphs} candidate={candidate} />}
    {!!catalog?.archived_versions.length && <details><summary>已删除段落的历史（{catalog.archived_versions.length} 个版本）</summary>
      {catalog.archived_versions.map((version) => <article className="version-card" key={version.id}><p>原段 {version.paragraph_id.slice(0, 8)} · 版本 {version.number} · 已从当前稿删除 · {version.is_simulated ? '模拟' : '真实生成'}</p>
        {version.available ? <VersionAudio projectId={projectId} version={version} /> : <p>音频文件不可用</p>}</article>)}</details>}
  </section>;
}
