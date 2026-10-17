import type { TextDocument } from './TextPanel';

// Unsaved input survives project navigation within this window, not a page reload.
export const pendingDrafts = new Map<string, { doc: TextDocument; saved: string }>();
