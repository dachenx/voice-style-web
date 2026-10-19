import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowDown, ArrowRight, AudioLines, Check, ChevronRight, CircleAlert, Folder,
  FolderOpen, HardDrive, Layers3, LoaderCircle, Pencil, Plus, RefreshCw, X } from 'lucide-react';
import { ApiError, messageOf, request, validateName, type Project } from './api';
import { forgetProjectOperations, setCreation, useCreation } from './operationCache';
import DeleteProjectDialog, { readDeleteIntent } from './DeleteProjectDialog';
import ReferencePanel from './ReferencePanel';
import TextPanel from './TextPanel';
import { pendingDrafts } from './textDraftCache';
import { transcriptDrafts } from './transcriptDraftCache';

type Detail = { state: 'loading' } | { state: 'error'; message: string; deleted?: boolean } |
  { state: 'ready'; project: Project };
type Editing = { mode: 'create' } | { mode: 'rename'; project: Project };

const getSelectedId = () => new URLSearchParams(window.location.hash.slice(1)).get('project');
const dateOf = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
}).format(new Date(value));
const sorted = (items: Project[]) => [...items].sort((a, b) =>
  b.updated_at.localeCompare(a.updated_at) || a.id.localeCompare(b.id));

function NameDialog({ editing, onDismiss, onSaved }: {
  editing: Editing; onDismiss: () => void; onSaved: (project: Project) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const pending = useCreation();
  const [name, setName] = useState(editing.mode === 'rename' ? editing.project.name : pending?.name ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const validation = validateName(name);
  const creating = editing.mode === 'create';

  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement as HTMLElement | null;
    element.showModal();
    input.current?.focus();
    input.current?.select();
    return () => { element.close(); previous?.focus(); };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saving || validation) return;
    setSaving(true);
    setError(null);
    const creationBody = creating ? pending ?? { request_id: crypto.randomUUID(), name: name.trim() } : null;
    if (creationBody) setCreation(creationBody);
    try {
      const project = await request<Project>(creating ? '/projects' : `/projects/${editing.project.id}`, {
        method: creating ? 'POST' : 'PATCH', body: JSON.stringify(creationBody ?? { name: name.trim() }),
      });
      if (creating) setCreation(null);
      onSaved(project);
    } catch (failure) {
      if (creating && failure instanceof ApiError && (failure.status === 422 || failure.code === 'project_deleted')) {
        setCreation(null); setError(failure.code === 'project_deleted' ? '原创建请求对应的项目已永久删除。可输入名称重新新建，旧项目不会恢复。' : messageOf(failure)); setSaving(false); return;
      }
      setError(`${messageOf(failure)}${creating ? " 创建结果请核对，重试会沿用原请求，不会重复创建。" : ""}`);
      setSaving(false);
    }
  }

  return <dialog ref={dialog} aria-labelledby="dialog-title" onCancel={(event) => {
    event.preventDefault(); if (!saving) onDismiss();
  }}>
    <form onSubmit={submit}>
      <div className="dialog-heading">
        <div className="icon-tile"><Folder size={24} /></div>
        <button type="button" className="icon-button" aria-label="关闭对话框" disabled={saving} onClick={onDismiss}><X size={20} /></button>
      </div>
      <h2 id="dialog-title">{creating ? '开启一个声音项目' : '重命名项目'}</h2>
      <p className="muted">{creating ? '给这次配音起个名字，让想法有一个开始。' : '新的名称将保存在本机，项目内容会继续保留。'}</p>
      <label className="field-label" htmlFor="project-name">项目名称</label>
      <input ref={input} id="project-name" value={name} disabled={saving || (creating && !!pending)} placeholder="例如：武汉街巷 · 开场白"
        autoComplete="off" aria-describedby="name-hint" aria-invalid={!!name && !!validation}
        onChange={(event) => { setName(event.target.value); setError(null); }} />
      <p id="name-hint" className={`field-hint ${name && validation ? 'invalid' : ''}`}>
        {name && validation ? validation : '1–80 个字，可以使用中文、数字和表情。'}
      </p>
      {creating && pending && <p className="field-hint">有待核对的创建请求，名称已固定。可重试原请求；关闭窗口后再点新建可继续核对。刷新整个页面前请先核对项目列表。</p>}
      {error && <div className="error-box" role="alert"><CircleAlert size={18} />{error}</div>}
      <div className="dialog-actions">
        <button type="button" className="button secondary" disabled={saving} onClick={onDismiss}>取消</button>
        <button type="submit" className="button primary" disabled={saving || !!validation}>
          {saving && <LoaderCircle size={16} className="spin" />}{saving ? '正在保存…' : creating ? '创建项目' : '保存名称'}
        </button>
      </div>
    </form>
  </dialog>;
}

export default function App() {
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (pendingDrafts.size > 0 || transcriptDrafts.size > 0) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, []);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [selectedId, setSelectedId] = useState(getSelectedId);
  const [detail, setDetail] = useState<Detail>({ state: 'loading' });
  const [editing, setEditing] = useState<Editing | null>(null);
  const [notice, setNotice] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [deleteIntent, setDeleteIntent] = useState(readDeleteIntent);
  const [archiving, setArchiving] = useState(false);
  const writeEpoch = useRef(0);

  useEffect(() => {
    const change = () => { setDetail({ state: 'loading' }); setSelectedId(getSelectedId()); };
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const epoch = writeEpoch.current;
    request<{ projects: Project[] }>(showArchived ? '/projects?archived=true' : '/projects', { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted || epoch !== writeEpoch.current) return;
        setProjects(sorted(result.projects)); setListError(null);
      })
      .catch((error: unknown) => { if (!controller.signal.aborted && epoch === writeEpoch.current) setListError(messageOf(error)); })
      .finally(() => { if (!controller.signal.aborted && epoch === writeEpoch.current) setLoading(false); });
    return () => controller.abort();
  }, [refresh, showArchived]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    const epoch = writeEpoch.current;
    request<Project>(`/projects/${encodeURIComponent(selectedId)}`, { signal: controller.signal })
      .then((project) => {
        if (!controller.signal.aborted && epoch === writeEpoch.current) setDetail({ state: 'ready', project });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && epoch === writeEpoch.current) setDetail({ state: 'error', message: messageOf(error), deleted: error instanceof ApiError && error.code === 'project_deleted' });
      });
    return () => controller.abort();
  }, [selectedId, refresh]);

  function changeFilter(archived: boolean) {
    if (archived === showArchived) return;
    writeEpoch.current += 1; setProjects([]); setLoading(true); setListError(null);
    setShowArchived(archived); setRefresh(v => v + 1);
  }

  function retry() {
    setLoading(true);
    setDetail({ state: 'loading' });
    setRefresh((value) => value + 1);
  }

  function saved(project: Project) {
    // Invalidate reads started before this write, then refresh authoritative server state.
    writeEpoch.current += 1;
    setRefresh((value) => value + 1);
    setProjects((items) => sorted([...(!!project.archived === showArchived ? [project] : []), ...items.filter((item) => item.id !== project.id)]));
    setDetail({ state: 'ready', project });
    setNotice(editing?.mode === 'create' ? '项目已创建并保存在本机。' : '项目名称已保存。');
    setEditing(null);
    window.location.hash = new URLSearchParams({ project: project.id }).toString();
  }

  async function archiveProject(project: Project) {
    if (archiving) return;
    if (pendingDrafts.has(project.id) || [...transcriptDrafts.keys()].some(key => key.startsWith(`${project.id}:`))) {
      setNotice('有未保存文案或参考原话，请先保存，再归档或恢复项目。'); return;
    }
    const archived = !project.archived;
    if (!window.confirm(archived ? '归档项目将从活跃列表移出，保留所有资料，可随时恢复。排队和运行中的任务会继续。是否归档？' : '恢复项目到活跃列表？')) return;
    setArchiving(true);
    writeEpoch.current += 1;
    try {
      await request(`/projects/${project.id}/archive`, { method: 'PUT', body: JSON.stringify({ archived }) });
      setNotice(archived ? '项目已归档，资料与任务保留，可在已归档列表恢复。' : '项目已恢复到活跃列表。');
      if (getSelectedId() === project.id) window.location.hash = '';
    } catch (cause) {
      setNotice(`项目整理结果需要核对：${messageOf(cause)} 请刷新列表核对；归档与恢复按按钮标明的目标执行。`);
    } finally { setArchiving(false); setRefresh(v => v + 1); }
  }

  function startDelete(project: Project) {
    if (deleteIntent && deleteIntent.id !== project.id) { setNotice('请先核对上一次删除，再删除其他项目。'); return; }
    if (pendingDrafts.has(project.id) || [...transcriptDrafts.keys()].some(key => key.startsWith(`${project.id}:`))) {
      setNotice('有未保存文案或参考原话，请先保存或明确重新载入后再删除。'); return;
    }
    setDeleting(project);
  }
  function removed(id: string, complete: boolean) {
    writeEpoch.current += 1;
    pendingDrafts.delete(id);
    for (const key of transcriptDrafts.keys()) if (key.startsWith(`${id}:`)) transcriptDrafts.delete(key);
    forgetProjectOperations(id);
    setProjects(items => items.filter(p => p.id !== id));
    if (getSelectedId() === id) { window.location.hash = ''; setSelectedId(null); }
    setRefresh(v => v + 1);
    setNotice(complete ? '项目及其应用内文件已永久删除。' : '项目已移除，文件清理尚未完成，请继续核对删除。');
  }

  function contentChanged() {
    writeEpoch.current += 1;
    setRefresh((value) => value + 1);
  }

  return <div className="app-shell">
    <aside className="sidebar" aria-label="工作空间导航">
      <a className="brand" href="#" aria-label="声作首页">
        <span className="brand-mark"><AudioLines size={26} strokeWidth={2.2} /></span>
        <span>声作<small>VOICE STUDIO</small></span>
      </a>
      <div className="sidebar-caption">工作空间</div>
      <a href="#" className="nav-item active" aria-current="page"><Layers3 size={19} />我的项目<span>{projects.length}</span></a>
      <div className="sidebar-bottom">
        <div className="local-icon"><HardDrive size={19} /></div>
        <div><strong>本地工作空间</strong><p>项目保存在这台电脑</p></div>
      </div>
    </aside>

    <main className="main">
      <header className="topbar">
        <div className="breadcrumb">工作空间 <ChevronRight size={14} /><span>我的项目</span></div>
        <span className={`connection ${listError ? 'offline' : ''}`}>
          <i />{loading ? '正在连接' : listError ? '连接异常' : '本地服务已连接'}
        </span>
      </header>

      <div className="page-content">
        <section className="page-heading">
          <div><p className="eyebrow">YOUR VOICE, YOUR STORIES</p><h1>我的项目<span className="heading-dot">.</span></h1>
            <p className="subtitle">让每一段文案，拥有自己的声音。</p></div>
          <button className="button primary" onClick={() => setEditing({ mode: 'create' })}><Plus size={18} />新建项目</button>
        </section>

        <section className="intro-card" aria-label="使用流程指南">
          <div className="intro-copy"><span className="pill"><span className="tiny-dot" />使用流程指南</span>
            <h2>把声音的想法，<br />留在这里。</h2>
            <p>参考声音 → 文案与人工确认 → 生成试听 → 版本选用 → 导出。<br className="desktop-break" />这是操作流程；当前状态请查看各工作区提示。</p></div>
          <div className="sound-art" aria-hidden="true"><div className="orbit orbit-one" /><div className="orbit orbit-two" />
            <div className="sound-card"><div className="sound-card-top"><AudioLines size={17} /><span>VOICE NOTES</span><span className="tiny-dot" /></div>
              <div className="waveform">{[18, 30, 47, 25, 60, 77, 48, 89, 63, 38, 68, 49, 29, 57, 39, 22, 12].map((height, index) => <i key={index} style={{ height }} />)}</div>
              <div className="sound-card-bottom"><span>每个声音，都值得被听见</span><span>01</span></div></div>
            <div className="sound-label"><Check size={13} />灵感已就位</div>
          </div>
        </section>

        <div className="section-heading"><div><h2>{showArchived ? '已归档项目' : '活跃项目'}</h2><span className="count">{projects.length}</span></div>
          <span className="sort-note"><ArrowDown size={13} />最近更新</span></div>
        <div className="text-actions"><button className="button secondary" aria-pressed={!showArchived} onClick={() => changeFilter(false)}>活跃项目</button><button className="button secondary" aria-pressed={showArchived} onClick={() => changeFilter(true)}>已归档</button><button className="text-button" onClick={retry}>刷新项目列表</button></div>
        {deleteIntent && <div className="error-box">有待核对的永久删除：{deleteIntent.name}<button className="text-button" onClick={() => setDeleting(deleteIntent)}>继续核对删除</button></div>}
        <p className="save-notice" role="status" aria-label="项目操作状态">{notice}</p>
        {listError && <div className="error-box list-error" role="alert"><CircleAlert size={19} /><span>{listError}</span>
          <button className="text-button" onClick={retry} disabled={loading}><RefreshCw size={14} />重试</button></div>}

        <div className={`workspace-grid ${selectedId ? 'has-selection' : ''}`}>
          <section className="project-list" aria-label="项目列表" aria-busy={loading}>
            {loading && projects.length === 0 ? <div className="empty-state"><LoaderCircle className="spin" size={24} /><p>正在读取本地项目…</p></div> :
              projects.length === 0 && !listError ? <div className="empty-state">
                <div className="empty-art"><FolderOpen size={33} strokeWidth={1.5} /><span><Plus size={12} /></span></div>
                <h3>{showArchived ? '暂无归档项目' : '暂无活跃项目'}</h3><p>{showArchived ? '归档仅整理列表，不删除资料或停止任务。' : '可以新建项目，或到已归档列表恢复已有项目。'}</p>
                <button className="button secondary" onClick={() => showArchived ? changeFilter(false) : setEditing({ mode: 'create' })}>{showArchived ? '查看活跃项目' : '创建项目'}<ArrowRight size={16} /></button>
              </div> : <div className="project-cards">{projects.map((project) =>
                <a key={project.id} className={`project-card ${project.id === selectedId ? 'selected' : ''}`}
                  href={`#project=${project.id}`} aria-current={project.id === selectedId ? 'true' : undefined}>
                  <div className="project-card-top"><div className="project-icon"><Folder size={21} /></div><span className="project-tag">{project.archived ? '已归档' : '项目已建立'}</span></div>
                  <h3>{project.name}</h3><p>更新于 <time dateTime={project.updated_at}>{dateOf(project.updated_at)}</time></p>
                  <div className="project-card-bottom"><span><HardDrive size={13} />保存在本机</span><ArrowRight size={17} /></div>
                </a>)}</div>}
          </section>

          {selectedId && <section className="detail-panel" aria-label="项目详情" aria-busy={detail.state === 'loading'}>
            <div className="detail-heading"><span>项目详情</span><a href="#" className="icon-button" aria-label="关闭项目详情"><X size={18} /></a></div>
            {detail.state === 'loading' ? <p className="detail-loading"><LoaderCircle className="spin" size={18} />正在读取项目…</p> :
              detail.state === 'error' ? <div className="detail-error"><p role="alert">{detail.message}</p>{detail.deleted && <button className="button secondary" onClick={() => { if (deleteIntent && deleteIntent.id !== selectedId) { setNotice('请先核对上一次删除，再处理其他项目。'); return; } const target = { id: selectedId!, name: '已删除项目', created_at: '', updated_at: '' }; setDeleteIntent(target); setDeleting(target); }}>核对删除清理状态</button>}<button className="button secondary" onClick={retry}>重新读取</button></div> : <>
                <div className="detail-icon"><FolderOpen size={27} /></div>
                <h2 className="detail-name">{detail.project.name}</h2>
                <button className="text-button rename-button" onClick={() => setEditing({ mode: 'rename', project: detail.project })}><Pencil size={14} />重命名</button>
                <button className="button secondary" disabled={archiving} onClick={() => void archiveProject(detail.project)}>{detail.project.archived ? '恢复项目' : '归档项目'}</button>
                <button className="button secondary" disabled={archiving} onClick={() => startDelete(detail.project)}>永久删除项目</button>
                {!!detail.project.archived && <p>该项目已归档，仅从活跃列表移出；资料仍可访问，任务不受影响。</p>}
                <dl className="project-meta"><div><dt>创建时间</dt><dd><time dateTime={detail.project.created_at}>{dateOf(detail.project.created_at)}</time></dd></div>
                  <div><dt>最近更新</dt><dd><time dateTime={detail.project.updated_at}>{dateOf(detail.project.updated_at)}</time></dd></div><div><dt>保存位置</dt><dd>本机</dd></div></dl>
                <div className="next-step"><span className="step-number">继续操作</span><p>在下方查看参考声音、文案、任务与版本。各工作区会提示需要处理的事项；历史音频不代表当前稿已完成。</p></div>
                <p className="saved-caption"><Check size={13} />项目信息已保存，刷新或重启后可继续</p>
              </>}
          </section>}
        </div>
        {selectedId && detail.state === 'ready' && <ReferencePanel key={`reference-${selectedId}`} project={detail.project}
          onChanged={contentChanged} />}
        {selectedId && <TextPanel key={`text-${selectedId}`} projectId={selectedId} onChanged={contentChanged} referenceRefresh={refresh} />}
        <footer className="page-footer"><span><HardDrive size={14} />本地保存 · 随时继续</span><span>文案整理 · 生成试听 · 版本选用 · 导出</span></footer>
      </div>
    </main>
    {deleting && <DeleteProjectDialog key={deleting.id} project={deleting} retry={deleteIntent?.id === deleting.id} onClose={() => setDeleting(null)} onRemoved={removed} onPending={setDeleteIntent} />}
    {editing && <NameDialog editing={editing} onDismiss={() => setEditing(null)} onSaved={saved} />}
  </div>;
}
