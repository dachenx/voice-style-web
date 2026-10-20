import { useState } from 'react';
import type { Paragraph } from './TextPanel';

// Keep text and punctuation; trim boundary whitespace so no empty paragraph is created.
// Prefer sentence boundaries,
// then clause boundaries; an overlong clause needs a visible hard split for review.
export function splitText(text: string, limit = 80): string[] {
  const chars = Array.from(text);
  const result: string[] = [];
  while (chars.length > limit) {
    let end = 0;
    for (const pattern of [/[。！？!?\n]/, /[，,；;、：:\s]/]) {
      for (let i = limit - 1; i >= 0; i--) {
        if (pattern.test(chars[i]) && chars.slice(0, i + 1).join('').trim()) { end = i + 1; break; }
      }
      if (end) break;
    }
    result.push(chars.splice(0, end || limit).join(''));
  }
  if (chars.length) result.push(chars.join(''));
  return result.map(part => part.trim()).filter(Boolean);
}
export default function TextSplit({ paragraphs, disabled, onApply }: {
  paragraphs: Paragraph[]; disabled: boolean; onApply: (paragraphs: Paragraph[]) => void;
}) {
  const [preview, setPreview] = useState<{ before: string; after: Paragraph[] } | null>(null);
  const [undo, setUndo] = useState<{ after: string; before: Paragraph[] } | null>(null);
  const [error, setError] = useState('');
  const current = JSON.stringify(paragraphs);
  const stale = preview?.before !== current;
  const long = paragraphs.some(p => Array.from(p.draft_text).length > 80);
  function prepare() {
    const after = paragraphs.flatMap(p => {
      if (!p.draft_text.trim() || Array.from(p.draft_text).length <= 80) return [p];
      const same = !p.original_text.trim() || p.original_text === p.draft_text;
      return splitText(p.draft_text).map((text, i) => i === 0 ? { ...p, draft_text: text, original_text: same ? text : p.original_text } : {
        id: crypto.randomUUID(), original_text: text, draft_text: text, pronunciation_note: '',
      });
    });
    if (after.length > 200) { setError('分段后超过 200 段，请缩短文案或分批处理。'); return; }
    if (after.reduce((sum, p) => sum + Array.from(p.original_text.trim() ? p.original_text : p.draft_text).length, 0) > 20000) { setError('保留原稿后超过整篇原稿 20000 字上限，请先整理高级编辑中的原稿或分批处理。'); return; }
    setError(''); setPreview({ before: current, after });
  }
  return <div className="text-split">
    <button className="text-button" disabled={disabled || !long} onClick={prepare}>长文自动分段（预览）</button>
    <p className="field-hint">人物初版每段最多 80 字（含标点）。优先按句末或逗号分段；超长句会切开，请核对断句。分段边界的空白会移除，不改写文字、不自动生成。</p>
    {error && <p role="alert">{error}</p>}
    {preview && <section aria-label="自动分段预览"><p>共 {preview.after.length} 段。原稿与朗读稿相同时一起分段；不同时首段保留原稿。首段保留备注及身份；新增段会作为新音频段落，需重新生成和选用。旧音频与历史仍保留。</p>
      <ol>{preview.after.map(p => <li key={p.id}>{p.draft_text}（{Array.from(p.draft_text).length} 字）</li>)}</ol>
      {stale && <p role="alert">文案已经改变，请重新预览。</p>}
      <button className="button secondary" disabled={disabled || stale} onClick={() => {
        setUndo({ before: paragraphs, after: JSON.stringify(preview.after) }); onApply(preview.after); setPreview(null);
      }}>应用分段</button><button className="text-button" onClick={() => setPreview(null)}>取消分段</button></section>}
    {undo && <button className="text-button" disabled={disabled || undo.after !== current} onClick={() => { onApply(undo.before); setUndo(null); }}>撤销自动分段</button>}
  </div>;
}
