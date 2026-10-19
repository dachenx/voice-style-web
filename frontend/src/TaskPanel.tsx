import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, messageOf, request } from './api';
import type { Clip, ReferenceAudio } from './ReferencePanel';
import type { TextDocument } from './TextPanel';
import VersionPanel from './VersionPanel';
import ReferenceTranscriptEditor, { type TranscriptReadiness } from './ReferenceTranscript';
import type { Adapter, AdapterHealth, Task, TaskDetail, TaskInput, WorkerHealth } from './taskTypes';

type Library = { references: ReferenceAudio[]; active_clip_id: string | null };
type Pending = { kind: 'create'; input: TaskInput } | { kind: 'retry'; taskId: string; input: { request_id: string } };
const labels: Record<Task['state'], string> = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', canceled: '已取消' };
const phases: Record<string, string> = { queued: '等待工作进程', validating: '检查固定输入', generating: '制作合成测试音', finalizing: '验证并保存文件', succeeded: '结果已保存', failed: '处理失败', canceled: '排队已取消' };
const pendingKey = (projectId: string) => `voice-task-pending:${projectId}`;
function restore(projectId: string): Pending | null {
  try {
    const stored: unknown = JSON.parse(sessionStorage.getItem(pendingKey(projectId)) ?? 'null');
    if (!stored || typeof stored !== 'object' || !('kind' in stored) || !('input' in stored)) return null;
    const input = stored.input;
    if (!input || typeof input !== 'object' || !('request_id' in input) || typeof input.request_id !== 'string') return null;
    if (stored.kind === 'create' || (stored.kind === 'retry' && 'taskId' in stored && typeof stored.taskId === 'string')) return stored as Pending;
  } catch { /* A malformed local recovery entry is never sent automatically. */ }
  return null;
}
const merge = (old: Task[], incoming: Task[]) => {
  const values = new Map(old.map((task) => [task.id, task]));
  for (const task of incoming) {
    if ((values.get(task.id)?.state_version ?? -1) <= task.state_version) values.set(task.id, task);
  }
  return [...values.values()].sort((a, b) => b.created_at.localeCompare(a.created_at) || a.id.localeCompare(b.id));
};

function ResultAudio({ url, simulated }: { url: string; simulated: boolean }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  return <div className="audio-player"><audio key={attempt} controls preload="metadata" src={url}
    aria-label={simulated ? '模拟任务结果' : '真实语音任务结果'} onError={() => setFailed(true)} />
    {failed && <p role="alert" className="error-box">音频无法读取，请核对本地服务和结果文件。
      <button className="text-button" onClick={() => { setFailed(false); setAttempt((value) => value + 1); }}>{simulated ? '重载模拟音频' : '重载语音'}</button></p>}</div>;
}

export default function TaskPanel({ projectId, doc, dirty, textBusy, textError, referenceRefresh, onChanged }: {
  projectId: string; doc: TextDocument | null; dirty: boolean; textBusy: boolean; textError: boolean; referenceRefresh: number;
  onChanged: () => void;
}) {
  const base = `/projects/${projectId}/tasks`;
  const [tasks, setTasks] = useState<Task[]>([]);
  const [worker, setWorker] = useState<WorkerHealth | null>(null);
  const [listError, setListError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [library, setLibrary] = useState<Library | null>(null);
  const [adapters, setAdapters] = useState<Adapter[]>([]);
  const [adapterId, setAdapterId] = useState('simulated-tone');
  const [adapterHealth, setAdapterHealth] = useState<{ value: AdapterHealth; stamp: string } | null>(null);
  const [healthError, setHealthError] = useState('');
  const [transcript, setTranscript] = useState<TranscriptReadiness | null>(null);
  const [inputError, setInputError] = useState('');
  const [inputReady, setInputReady] = useState(false);
  const [inputStamp, setInputStamp] = useState('');
  const [inputRefresh, setInputRefresh] = useState(0);
  const [paragraphId, setParagraphId] = useState('');
  const [pending, setPending] = useState<Pending | null>(() => restore(projectId));
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  const lifetime = useRef<AbortController | null>(null);
  const listOperation = useRef(0);
  const detailOperation = useRef(0);
  const inputOperation = useRef(0);
  const [pollTick, setPollTick] = useState(0);
  const adapter = adapters.find((item) => item.id === adapterId);
  const real = adapter !== undefined && !adapter.is_simulated;
  const stamp = `${referenceRefresh}:${inputRefresh}`;
  const health = adapterHealth?.value.adapter_id === adapterId && adapterHealth.stamp === stamp ? adapterHealth.value : null;
  const selected = library?.references.flatMap((reference) => reference.clips.map((clip) => ({ reference, clip })))
    .find(({ clip }) => clip.id === library.active_clip_id);
  const confirmed = doc?.latest_confirmation;
  const selectedParagraph = doc?.paragraphs.find((paragraph) => paragraph.id === paragraphId) ?? doc?.paragraphs[0];
  const blocked = !doc ? '等待文案读取。' : textBusy ? '请等待文案操作完成。' : dirty ? '有未保存文案，请先保存并重新确认。' :
    textError ? '文案读取或保存出错，请先处理文案工作台中的提示。' :
      !doc.confirmation_current || !confirmed ? '请先保存完整文案并人工确认。' :
        !inputReady || inputStamp !== `${referenceRefresh}:${inputRefresh}` ? '等待参考与组件读取，请稍后或刷新重试。' : !selected ? '请先在参考声音中保存并选择当前片段。' :
          !adapter ? '生成组件不可用，请刷新组件信息。' :
            real && (!health || !health.execution_available || healthError) ? '真实模型尚不可调用，请先处理下方的模型状态提示。' :
              real && adapter.reference_max_duration_ms !== undefined && selected.clip.end_ms - selected.clip.start_ms > adapter.reference_max_duration_ms ?
                `参考片段超过该模型的 ${adapter.reference_max_duration_ms / 1000} 秒限制，请重新选段。` :
                real && (!transcript?.ready || transcript.clipId !== selected.clip.id) ? '请填写、核对并保存当前参考片段的准确原话。' : '';

  // This guides only the generation step; history never proves the current draft is complete.
  const activeTasks = tasks.filter((task) => task.state === 'queued' || task.state === 'running');
  const nextStep = busy ? '正在处理任务操作，请等待结果。' :
    pending ? '上次提交结果未知，请先使用“核对并重试上次提交”，不要重复新建任务。' :
      actionError ? '上次任务操作有错误，请先核对下方“任务操作错误”提示，再按提示刷新或重试。' :
      listError ? '任务状态读取失败，请使用“刷新任务”核对；已显示记录可能不是最新状态。' :
        !loaded ? '正在读取任务状态，请稍候。' :
          worker?.error ? '工作进程不可用，请处理下方错误并重启本地服务后核对；已保存的队列仍保留。' :
            inputError ? '参考或组件读取失败，请使用“刷新参考与组件”重试。' :
              textError ? '文案读取或保存出错，请先处理文案工作台中的提示。' :
                blocked || (activeTasks.length ? `本项目有 ${activeTasks.length} 个排队或运行任务，请在任务记录查看段落进度；生成结果仍需试听。` :
                  tasks.some((task) => task.state === 'succeeded') ? '已有生成记录。请在版本区核对是否匹配当前稿，试听后逐段选用，再到“拼接与导出”核对缺段和过时提示。模拟音不代表真实配音。' :
                    tasks.length ? '请查看任务记录中的失败或取消原因。按原输入重试会沿用旧快照；需要新内容时请核对当前文案后提交新任务。' :
                      '当前生成输入已就绪。可先提交短段测试，也可提交全文；生成后仍需试听并逐段选用。模拟模式只生成测试音。');

  useEffect(() => {
    const abort = new AbortController();
    lifetime.current = abort;
    return () => { abort.abort(); listOperation.current += 1; detailOperation.current += 1; inputOperation.current += 1; };
  }, [projectId]);

  useEffect(() => {
    const abort = new AbortController();
    const version = ++inputOperation.current;
    Promise.all([
      request<Library>(`/projects/${projectId}/references`, { signal: abort.signal }),
      request<{ adapters: Adapter[] }>('/generation-adapters', { signal: abort.signal }),
    ]).then(([value, capabilities]) => {
      if (abort.signal.aborted || version !== inputOperation.current) return;
      setLibrary(value); setAdapters(capabilities.adapters); setInputReady(true); setInputStamp(`${referenceRefresh}:${inputRefresh}`); setInputError('');
    }).catch((cause) => {
      if (abort.signal.aborted || version !== inputOperation.current) return;
      setInputReady(false); setInputError(messageOf(cause));
    });
    return () => { abort.abort(); };
  }, [projectId, referenceRefresh, inputRefresh]);

  useEffect(() => {
    if (adapterId === 'simulated-tone') return;
    const abort = new AbortController();
    request<AdapterHealth>(`/generation-adapters/${encodeURIComponent(adapterId)}/health`, { signal: abort.signal }).then((value) => {
      if (!abort.signal.aborted) { setAdapterHealth({ value, stamp }); setHealthError(''); }
    }).catch((cause) => { if (!abort.signal.aborted) setHealthError(messageOf(cause)); });
    return () => { abort.abort(); };
  }, [adapterId, stamp]);

  const readTasks = useCallback(async () => {
    const abort = lifetime.current;
    if (!abort || abort.signal.aborted) return;
    const version = ++listOperation.current;
    try {
      const value = await request<{ tasks: Task[]; worker: WorkerHealth }>(base, { signal: abort.signal });
      if (abort.signal.aborted || version !== listOperation.current) return;
      setTasks((old) => merge(old, value.tasks)); setWorker(value.worker); setListError(''); setLoaded(true);
    } catch (cause) {
      if (!abort.signal.aborted && version === listOperation.current) setListError(`任务刷新失败：${messageOf(cause)} 已显示的任务保留，请刷新核对。`);
    }
  }, [base]);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await readTasks();
      if (!disposed) timer = setTimeout(() => { void poll(); }, 1500);
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); listOperation.current += 1; };
  }, [readTasks, pollTick]);

  function retain(value: Pending) {
    // Save the same key before sending. A lost response can be reconciled after navigation/reload.
    setPending(value);
    sessionStorage.setItem(pendingKey(projectId), JSON.stringify(value));
  }
  function clearPending() {
    sessionStorage.removeItem(pendingKey(projectId)); setPending(null);
  }
  async function send(value: Pending) {
    const abort = lifetime.current;
    if (!abort || busyRef.current) return;
    busyRef.current = true; setBusy(true); setActionError(''); setNotice('');
    try {
      retain(value);
      listOperation.current += 1;
      const task = await request<TaskDetail>(value.kind === 'create' ? base : `${base}/${encodeURIComponent(value.taskId)}/retry`, {
        method: 'POST', body: JSON.stringify(value.input), signal: abort.signal,
      });
      if (abort.signal.aborted) return;
      clearPending(); listOperation.current += 1;
      setTasks((old) => merge(old, [task]));
      setNotice(value.kind === 'retry' ? '重试任务已保存，继续使用原任务的固定输入。' : task.is_simulated ? '模拟任务已保存到本机队列。' : '真实语音任务已保存到本机队列。');
      onChanged();
      setPollTick((tick) => tick + 1);
    } catch (cause) {
      if (abort.signal.aborted) return;
      if (cause instanceof ApiError && [400, 404, 409, 422].includes(cause.status)) {
        clearPending(); setActionError(`${messageOf(cause)} 请刷新参考信息，并核对文案版本后重新提交。`);
      } else setActionError(`${messageOf(cause)} 提交结果尚未确认，请使用“核对并重试上次提交”，同一请求不会重复入队。`);
    } finally {
      busyRef.current = false;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  function submit(mode: 'preview' | 'full', explicitParagraphId?: string) {
    if (blocked || pending || busyRef.current || !doc || !confirmed || !selected || !adapter || !selectedParagraph) return;
    void send({ kind: 'create', input: { request_id: crypto.randomUUID(), expected_revision: doc.revision,
      expected_confirmation_id: confirmed.id, expected_clip_id: selected.clip.id, adapter_id: adapter.id,
      mode, paragraph_id: mode === 'preview' ? explicitParagraphId ?? selectedParagraph.id : null, parameters: {},
      ...(real ? { expected_transcript_revision: transcript!.value!.revision, expected_adapter_version: adapter.version,
        expected_config_fingerprint: adapter.config_fingerprint } : {}) } });
  }
  async function cancel(task: Task) {
    const abort = lifetime.current;
    if (!abort || busyRef.current) return;
    busyRef.current = true; setBusy(true); setActionError(''); setNotice(''); listOperation.current += 1;
    try {
      const value = await request<TaskDetail>(`${base}/${task.id}/cancel`, { method: 'POST', signal: abort.signal });
      if (abort.signal.aborted) return;
      listOperation.current += 1; setTasks((old) => merge(old, [value])); setNotice('排队任务已取消。');
    } catch (cause) {
      if (!abort.signal.aborted) setActionError(`${messageOf(cause)} 请刷新核对任务状态；已运行的任务不能取消。`);
    } finally {
      busyRef.current = false;
      if (!abort.signal.aborted) { setBusy(false); setPollTick((tick) => tick + 1); }
    }
  }
  async function showDetail(task: Task) {
    const abort = lifetime.current;
    if (!abort) return;
    const version = ++detailOperation.current;
    setDetailError('');
    try {
      const value = await request<TaskDetail>(`${base}/${task.id}`, { signal: abort.signal });
      if (!abort.signal.aborted && version === detailOperation.current) setDetail(value);
    } catch (cause) {
      if (!abort.signal.aborted && version === detailOperation.current) setDetailError(messageOf(cause));
    }
  }
  const clipRange = (clip: Pick<Clip, 'start_ms' | 'end_ms'>) => `${(clip.start_ms / 1000).toFixed(3)}–${(clip.end_ms / 1000).toFixed(3)} 秒`;

  return <section className="reference-panel task-panel" aria-label={real ? '真实语音与任务' : '模拟生成与任务'}>
    <div className="reference-heading"><div><span className="eyebrow">VOICE GENERATION</span><h2>{real ? '真实语音与任务' : '模拟生成与任务'}</h2>
      <p>{real ? '使用本机模型和当前参考片段生成语音。武汉话、人物相似度和自然度需要生成后逐项试听。' : '仅生成合成测试音，用于验证排队和恢复流程；不会朗读文案、复刻参考声音或生成武汉话。'}</p></div><span className="simulation-badge">{real ? '本机真实模型 · 效果待验' : '模拟模式'}</span></div>
    <div className="next-step" role="status" aria-label="生成环节下一步"><span className="step-number">生成环节 · 下一步</span><p>{nextStep}</p></div>
    <p className="audio-hint">建议先做短段测试，再将全部段落排队。当前适配器没有语速、情绪或读音备注控制。</p>
    <label className="audio-label" htmlFor={`generation-adapter-${projectId}`}>选择生成组件</label>
    <select id={`generation-adapter-${projectId}`} value={adapterId} disabled={busy || !!pending} onChange={(event) => {
      setAdapterId(event.target.value); setAdapterHealth(null); setHealthError('');
    }}>{adapters.map((item) => <option key={item.id} value={item.id}>{item.label}{item.is_simulated ? '（模拟）' : '（真实模型）'}</option>)}</select>
    <div className="task-input-summary">
      <p>确认文案：{doc?.confirmation_current && confirmed && !dirty ? `版本 ${confirmed.revision}` : '尚不可用'}</p>
      <p>当前参考：{selected ? `${selected.reference.filename} · ${clipRange(selected.clip)}` : '未选择'}</p>
      <p>生成组件：{adapter ? `${adapter.label} · ${adapter.version}` : '尚不可用'}</p>
      <button className="text-button" onClick={() => { setInputReady(false); setInputRefresh((value) => value + 1); }}>刷新参考与组件</button>
    </div>
    {inputError && <p role="alert" className="error-box">参考或组件读取失败：{inputError}</p>}
    {real && <div className="model-status" aria-label="真实模型状态">
      <p>{health ? !health.configured ? '模型未配置' : !health.configuration_valid ? '模型配置不可用' : health.execution_available ? '模型调用入口可用 · 任务执行时加载' : '模型调用入口尚不可用' : '正在检查模型环境…'}</p>
      <p className="audio-hint">{health?.model_loaded ? '本次检查时模型已加载；声音效果仍待试听。' : health?.last_loaded_at ? `最近加载成功：${new Date(health.last_loaded_at).toLocaleString('zh-CN')}。本次检查时模型未加载，声音效果仍待试听。` : '尚无已加载成功的证明；配置存在不代表声音可用。'} 使用“刷新参考与组件”更新状态。</p>
      <p className="audio-hint">模型：{adapter.model_id ?? '待配置'} · {adapter.model_revision ?? '版本待核对'}；参考片段最多 {(adapter.reference_max_duration_ms ?? 30000) / 1000} 秒。</p>
      {adapter.max_text_tokens !== undefined && <p className="audio-hint">每段最多 {adapter.max_text_tokens} 个模型 token，提交后由模型分词器核对；较长文案请在文案工作台分段。{adapter.limit_note}</p>}
      {health?.error && <p role="alert" className="error-box">{health.error.message}</p>}
      {healthError && <p role="alert" className="error-box">模型检查失败：{healthError}</p>}
    </div>}
    {real && selected && <ReferenceTranscriptEditor key={selected.clip.id} projectId={projectId}
      referenceId={selected.reference.id} clipId={selected.clip.id} onReady={setTranscript} onChanged={onChanged} />}
    <label className="audio-label" htmlFor={`task-paragraph-${projectId}`}>短段测试段落</label>
    <select id={`task-paragraph-${projectId}`} value={selectedParagraph?.id ?? ''} disabled={busy || !!pending || !!blocked}
      onChange={(event) => setParagraphId(event.target.value)}>
      {!doc?.paragraphs.length && <option value="">请先填写文案</option>}
      {doc?.paragraphs.map((paragraph, index) => <option key={paragraph.id} value={paragraph.id}>第 {index + 1} 段 · {paragraph.draft_text.slice(0, 32)}</option>)}
    </select>
    {blocked && <p className="field-hint">{blocked}</p>}
    <div className="task-actions"><button className="button primary" disabled={busy || !!pending || !!blocked} onClick={() => submit('preview')}>{real ? '提交真实短段任务' : '提交模拟短段任务'}</button>
      <button className="button secondary" disabled={busy || !!pending || !!blocked} onClick={() => submit('full')}>{real ? '提交真实全文任务' : '提交模拟全文任务'}</button></div>
    {pending && <div className="pending-task"><p>上次提交的结果尚未核对。恢复请求会使用当时的固定输入，不会改用当前编辑内容。</p>
      <button className="button secondary" disabled={busy} onClick={() => void send(pending)}>核对并重试上次提交</button></div>}
    {actionError && <p role="alert" aria-label="任务操作错误" className="error-box">{actionError}</p>}
    <p role="status" aria-label="任务操作状态" className="reference-notice">{notice}</p>
    <VersionPanel key={projectId} projectId={projectId} adapterId={adapterId} dirty={dirty}
      refreshStamp={`${doc?.revision}:${doc?.latest_confirmation?.id}:${referenceRefresh}:${inputRefresh}:${transcript?.value?.revision}:${tasks.filter((task) => task.state === 'succeeded').map((task) => task.id).join(',')}`}
      blocked={blocked || (pending ? '请先核对上次提交结果。' : busy ? '请等待任务操作完成。' : '')}
      onGenerate={(id) => submit('preview', id)} onChanged={onChanged} />
    <div className="subsection-title"><h3>任务记录</h3><button className="text-button" onClick={() => setPollTick((tick) => tick + 1)}>刷新任务</button></div>
    <p className="audio-hint">单任务工作进程 · {worker?.state === 'running' ? '正在处理任务' : worker?.state === 'idle' ? '已就绪' : '状态待核对'}。关闭页面不停止已保存的队列。</p>
    {worker?.error && <p role="alert" className="error-box">工作进程不可用：{worker.error.message} 队列记录仍保留，请重启本地服务后核对。</p>}
    {listError && <p role="alert" aria-label="任务读取错误" className="error-box">{listError}</p>}
    {!loaded && !listError && <p className="muted">正在读取任务…</p>}
    {loaded && !listError && !tasks.length && <p className="muted">尚无生成任务。</p>}
    <div className="task-list">{tasks.map((task) => <article className="task-card" key={task.id} data-task-id={task.id}>
      <div className="task-card-heading"><h4>{task.is_simulated ? '模拟' : '真实语音'}{task.mode === 'preview' ? '短段' : '全文'}任务</h4><strong className={`task-state task-${task.state}`}>{labels[task.state]}</strong></div>
      <p className="audio-hint">{new Date(task.created_at).toLocaleString('zh-CN')} · {task.adapter.id} / {task.adapter.version}</p>
      <p>{!task.is_simulated && task.phase === 'generating' ? '正在生成语音' : phases[task.phase] ?? task.phase} · 已处理 {task.completed_paragraphs} / {task.total_paragraphs} 段</p>
      <progress max={Math.max(1, task.total_paragraphs)} value={task.completed_paragraphs} aria-label={task.is_simulated ? '模拟任务段落进度' : '语音任务段落进度'} />
      {task.retry_of && <p className="audio-hint">由失败任务重试，使用原输入快照。</p>}
      {task.error && <p className="error-box">{task.error.message}（{task.error.code}）</p>}
      <div className="task-actions"><button className="text-button" onClick={() => void showDetail(task)}>查看任务快照</button>
        {task.state === 'queued' && <button className="text-button" disabled={busy} onClick={() => void cancel(task)}>取消排队</button>}
        {task.state === 'failed' && <button className="button secondary" disabled={busy || !!pending} onClick={() => void send({ kind: 'retry', taskId: task.id, input: { request_id: crypto.randomUUID() } })}>按原输入重试</button>}</div>
      {task.state === 'running' && <p className="audio-hint">正在运行的任务暂不支持取消。</p>}
      {task.state === 'succeeded' && task.result && <div className="task-result"><p>{task.is_simulated ? '模拟测试音' : '真实模型生成'} · {(task.result.duration_ms / 1000).toFixed(2)} 秒 · {task.is_simulated ? '不代表真实配音效果' : '内容、武汉话与音色待试听核对'}</p>
        <ResultAudio url={`/api${base}/${task.id}/audio`} simulated={task.is_simulated} /><a className="text-button" href={`/api${base}/${task.id}/download`} download>{task.is_simulated ? '下载模拟音频' : '下载生成语音'}</a></div>}
    </article>)}</div>
    {detailError && <p role="alert" className="error-box">任务详情读取失败：{detailError} 请重新点击查看。</p>}
    {detail && <section className="text-snapshot" aria-label="任务输入快照"><h3>固定输入（只读 · {detail.is_simulated ? '模拟任务' : '真实语音任务'}）</h3>
      <p>任务：{detail.id}</p><p>确认版本：{detail.snapshot.confirmation.revision} · {detail.snapshot.mode === 'preview' ? '指定短段' : '全部段落'}</p>
      <p>参考：{detail.snapshot.reference.filename} · {clipRange(detail.snapshot.reference.clip)}</p>
      <p className="audio-hint">参考片段：{detail.snapshot.reference.clip.id}</p>
      <p>组件：{detail.snapshot.adapter.id} / {detail.snapshot.adapter.version} · 未启用声音控制参数</p>
      {detail.snapshot.reference.transcript && <p>参考原话（版本 {detail.snapshot.reference.transcript.revision}）：{detail.snapshot.reference.transcript.text}</p>}
      {!detail.is_simulated && <><p>模型：{detail.snapshot.adapter.model_id} · {detail.snapshot.adapter.model_revision}</p>
        <details><summary>查看固定模型与配置摘要</summary><p className="snapshot-digest">模型：{detail.snapshot.adapter.model_fingerprint}</p><p className="snapshot-digest">配置：{detail.snapshot.adapter.config_fingerprint}</p></details></>}
      <p>语体：{detail.snapshot.confirmation.register} · 术语备注：{detail.snapshot.confirmation.terms || '无'}</p>
      {detail.snapshot.confirmation.paragraphs.map((paragraph, index) => <article key={paragraph.id}><h4>第 {index + 1} 段{detail.snapshot.paragraph_ids.includes(paragraph.id) ? '（本任务处理）' : ''}</h4>
        <p>原稿：{paragraph.original_text}</p><p>确认稿：{paragraph.draft_text}</p><p>读音备注：{paragraph.pronunciation_note || '无'}</p></article>)}
      <p className="audio-hint">后续文案或参考变化不会修改本任务；重试继续使用这里的原输入。</p></section>}
  </section>;
}
