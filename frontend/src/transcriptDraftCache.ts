import type { ReferenceTranscript } from './ReferenceTranscript';

// Window memory only. Keep the original CAS baseline when another window writes.
export const transcriptDrafts = new Map<string, { saved: ReferenceTranscript; text: string }>();
export const transcriptDraftKey = (projectId: string, clipId: string) => `${projectId}:${clipId}`;
