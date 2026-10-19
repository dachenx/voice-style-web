import { useSyncExternalStore } from 'react';

// Survives component remounts/project navigation in this window, not a page reload.
type Creation = { request_id: string; name: string } | null;
let creation: Creation = null;
const creationListeners = new Set<() => void>();
export function setCreation(value: Creation) { creation = value; creationListeners.forEach(fn => fn()); }
export function useCreation() {
  return useSyncExternalStore(fn => { creationListeners.add(fn); return () => { creationListeners.delete(fn); }; }, () => creation);
}
export type PendingExport = { body: string; paragraph: string | null };
type ExportOperation = { pause: number; pending: PendingExport | null; busy: boolean; completed: number; error: string; notice: string };
const exports = new Map<string, ExportOperation>();
const listeners = new Map<string, Set<() => void>>();
export function exportOperation(id: string): ExportOperation {
  if (!exports.has(id)) exports.set(id, { pause: 0, pending: null, busy: false, completed: 0, error: '', notice: '' });
  return exports.get(id)!;
}
export function updateExport(id: string, patch: Partial<ExportOperation>) {
  exports.set(id, { ...exportOperation(id), ...patch });
  listeners.get(id)?.forEach(fn => fn());
}
export function useExportOperation(id: string) {
  return useSyncExternalStore(fn => {
    if (!listeners.has(id)) listeners.set(id, new Set());
    listeners.get(id)!.add(fn);
    return () => { listeners.get(id)!.delete(fn); };
  }, () => exportOperation(id));
}
