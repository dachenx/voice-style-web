import { useEffect, useState } from 'react';
import { messageOf, request } from './api';
import AssetDeleteButton from './AssetDeleteButton';
type Pending = { kind: 'reference' | 'clip' | 'export'; asset_id: string; reference_id: string | null };
export default function AssetCleanupPanel({ projectId, exportsOnly = false, onChanged }: {
  projectId: string; exportsOnly?: boolean; onChanged: () => void;
}) {
  const [items, setItems] = useState<Pending[]>([]);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    request<Pending[]>(`/projects/${projectId}/asset-deletions`, { signal: abort.signal }).then(result => {
      if (!abort.signal.aborted) { setItems(result); setError(''); }
    }).catch(cause => { if (!abort.signal.aborted) setError(messageOf(cause)); });
    return () => abort.abort();
  }, [projectId, revision]);
  const filtered = items.filter(item => (item.kind === 'export') === exportsOnly);
  return <section aria-label={exportsOnly ? '导出清理状态' : '素材清理状态'}>
    <button className="text-button" onClick={() => setRevision(v => v + 1)}>检查{exportsOnly ? '导出' : '素材'}待清理文件</button>
    {error && <p role="alert">{error}</p>}
    {filtered.map(item => <div key={item.asset_id}><p>删除已登记，文件清理待完成：{item.asset_id}</p>
      <AssetDeleteButton title="重试清理" description="继续清理此前已经确认删除的应用内文件。"
        path={`/projects/${projectId}/${item.kind === 'export' ? `exports/${item.asset_id}` : item.kind === 'clip' ? `references/${item.reference_id}/clips/${item.asset_id}` : `references/${item.asset_id}`}`}
        onDeleted={() => { setRevision(v => v + 1); onChanged(); }} /></div>)}
  </section>;
}
