import { useEffect, useRef } from 'react';

export default function TaskDeleteDialog({ identity, busy, error, success, onConfirm, onClose, fallback }: {
  identity: string; busy: boolean; error: string; success: boolean;
  onConfirm: () => void; onClose: () => void; fallback: () => HTMLElement | null;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const fallbackRef = useRef(fallback);
  useEffect(() => {
    const element = dialog.current!;
    const restoreFallback = fallbackRef.current;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    element.showModal();
    cancel.current?.focus();
    return () => {
      element.close();
      (trigger?.isConnected && !trigger.matches(':disabled') ? trigger : restoreFallback())?.focus();
    };
  }, []);
  return <dialog ref={dialog} className="task-delete-dialog" aria-label="确认删除任务" tabIndex={-1}
    onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const first = buttons[0], last = buttons.at(-1);
      if (!first) { event.preventDefault(); event.currentTarget.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }}
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <h2>{success ? '删除已完成' : '永久删除这条任务及音频？'}</h2>
    <p className="muted">{identity}</p>
    <p>删除任务记录、已有的整篇和分段生成文件（如有）及对应候选版本。不会删除项目文案、参考声音、模型或其他任务。不可恢复。</p>
    {error && <p role="alert" aria-label="任务操作错误" className="error-box">{error}</p>}
    <p role="status" aria-label="任务操作状态">{busy ? '正在删除，请稍候…' : success ? '该任务记录和生成音频已永久删除。' : ''}</p>
    <div className="dialog-actions">
      <button ref={cancel} className="button secondary" disabled={busy} onClick={onClose}>{success ? '完成' : error ? '关闭提示' : '取消删除'}</button>
      {!success && <button className="button primary" disabled={busy} onClick={onConfirm}>{busy ? '正在删除…' : error ? '核对并重试本任务' : '确认永久删除任务'}</button>}
    </div>
  </dialog>;
}
