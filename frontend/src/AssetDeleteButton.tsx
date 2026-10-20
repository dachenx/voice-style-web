import { useEffect, useRef, useState } from 'react';
import { messageOf, request } from './api';

function Confirmation({ title, description, path, onDeleted, onClose }: {
  title: string; description: string; path: string; onDeleted: () => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const active = useRef(true);
  const sending = useRef(false);
  useEffect(() => {
    active.current = true;
    const element = dialog.current!;
    const trigger = document.activeElement as HTMLElement | null;
    element.showModal(); cancel.current?.focus();
    return () => { active.current = false; element.close(); if (trigger?.isConnected) trigger.focus(); };
  }, []);
  async function remove() {
    if (sending.current) return;
    sending.current = true; setBusy(true); setError('');
    try {
      await request(path, { method: 'DELETE', body: JSON.stringify({ acknowledged: true }) });
      if (active.current) { onDeleted(); onClose(); }
    } catch (cause) {
      if (active.current) setError(`${messageOf(cause)} 可以重试同一删除；响应中断时请刷新核对。`);
    } finally { sending.current = false; if (active.current) setBusy(false); }
  }
  return <dialog ref={dialog} className="task-delete-dialog" aria-label={`确认${title}`}
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <h2>{title}？</h2><p>{description}</p>
    {error && <p role="alert" className="error-box">{error}</p>}
    <div className="dialog-actions"><button ref={cancel} disabled={busy} className="button secondary" onClick={onClose}>取消删除</button>
      <button className="button primary" disabled={busy} onClick={() => void remove()}>{busy ? '正在删除…' : error ? '重试删除' : '确认永久删除'}</button></div>
  </dialog>;
}
export default function AssetDeleteButton(props: {
  title: string; description: string; path: string; disabled?: boolean; onDeleted: () => void;
}) {
  const [open, setOpen] = useState(false);
  return <><button className="text-button" disabled={props.disabled} onClick={() => setOpen(true)}>{props.title}</button>
    {open && <Confirmation {...props} onClose={() => setOpen(false)} />}</>;
}
