import { useEffect, useRef, useState } from 'react';
import { pendingDrafts } from './textDraftCache';
import { transcriptDrafts } from './transcriptDraftCache';
import { ApiError, messageOf, request, type Project } from './api';

const intentKey = 'voice-delete-intent';
export function readDeleteIntent(): Project | null {
  try { const p = JSON.parse(sessionStorage.getItem(intentKey) ?? 'null'); return p && typeof p.id === 'string' && typeof p.name === 'string' ? p : null; } catch { return null; }
}
function saveIntent(project: Project | null) {
  try { if (project) sessionStorage.setItem(intentKey, JSON.stringify(project)); else sessionStorage.removeItem(intentKey); } catch { /* Old project link can still query deletion status. */ }
}
export default function DeleteProjectDialog({ project, retry, onClose, onRemoved, onPending }: {
  project: Project; retry: boolean; onPending: (project: Project | null) => void; onClose: () => void; onRemoved: (id: string, complete: boolean) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(retry ? project.name : '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(retry);
  useEffect(() => { const el = dialog.current!; el.showModal(); return () => el.close(); }, []);
  async function remove() {
    if (busy) return;
    setBusy(true); setError(''); saveIntent(project); onPending(project);
    let checking = pending;
    let knownDeleted = false;
    try {
      if (pending) {
        const status = await request<{ deleted: boolean; cleanup_complete: boolean }>(`/projects/${project.id}/deletion-status`);
        knownDeleted = status.deleted;
        if (status.cleanup_complete) { saveIntent(null); onPending(null); onRemoved(project.id, true); onClose(); return; }
      }
      if (!knownDeleted && (pendingDrafts.has(project.id) || [...transcriptDrafts.keys()].some(key => key.startsWith(`${project.id}:`)))) {
        setError('项目仍存在，且有未保存文案或参考原话。请关闭窗口先保存或明确重新载入，再继续核对删除。'); return;
      }
      checking = false;
      await request(`/projects/${project.id}`, { method: 'DELETE', body: JSON.stringify({ confirm_name: project.name, acknowledged: true }) }, 60000);
      saveIntent(null); onPending(null); onRemoved(project.id, true); onClose();
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === 'delete_cleanup_pending') {
        setPending(true); onRemoved(project.id, false);
        setError(cause.message);
      } else if (!checking && !knownDeleted && cause instanceof ApiError && cause.status < 500 && !(pending && cause.code === 'project_busy')) {
        saveIntent(null); onPending(null); setPending(false); setError(messageOf(cause));
      } else {
        setPending(true); setError(`删除结果待核对：${messageOf(cause)} 请重试同一项目的删除；不要凭网络错误判断资料仍存在。`);
      }
    } finally { setBusy(false); }
  }
  return <dialog ref={dialog} aria-labelledby="delete-title" onCancel={e => { e.preventDefault(); if (!busy) onClose(); }}>
    <h2 id="delete-title">永久删除项目</h2>
    <p>即将删除「{project.name}」</p>
    <p>本项目文案、上传副本、参考片段、生成音频和导出将永久删除，不能恢复。</p>
    <p>Public 原始文件、模型、其他项目及独立备份不受影响。有排队、运行或其他进行中的操作时会阻止删除。</p>
    <label>输入项目名称以确认<input aria-label="输入项目名称以确认" value={name} disabled={busy || pending} onChange={e => setName(e.target.value)} /></label>
    {error && <p role="alert" className="error-box">{error}</p>}
    {pending && <p>保留本次删除目标供继续核对。刷新整页后，可从待核对入口或原项目链接重试清理；不要新建同名项目代替核对。</p>}
    <div className="dialog-actions">
      <button className="button secondary" disabled={busy} onClick={onClose}>{pending ? '稍后核对' : '取消'}</button>
      <button className="button primary" disabled={busy || name !== project.name} onClick={() => void remove()}>{busy ? '正在删除…' : pending ? '重试同一项目删除' : '确认永久删除'}</button>
    </div>
  </dialog>;
}
