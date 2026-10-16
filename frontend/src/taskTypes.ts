import type { TextDocument } from './TextPanel';

export type TaskInput = {
  request_id: string; expected_revision: number; expected_confirmation_id: string;
  expected_clip_id: string; adapter_id: string; mode: 'preview' | 'full';
  paragraph_id: string | null; parameters: Record<string, never>;
};
export type Adapter = { id: string; version: string; label: string; is_simulated: boolean };
export type WorkerHealth = { state: string; last_seen_at: string | null; error: { code: string; message: string } | null };
export type Task = {
  id: string; project_id: string; mode: 'preview' | 'full'; paragraph_id: string | null;
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'; phase: string;
  completed_paragraphs: number; total_paragraphs: number; state_version: number;
  is_simulated: boolean; adapter: { id: string; version: string }; retry_of: string | null;
  created_at: string; updated_at: string; started_at: string | null; finished_at: string | null;
  error: { code: string; message: string; phase?: string } | null;
  result: { filename: string; media_type: string; byte_size: number; sha256: string; duration_ms: number } | null;
};
export type TaskDetail = Task & {
  snapshot: {
    confirmation: NonNullable<TextDocument['latest_confirmation']>;
    reference: { id: string; filename: string; clip: { id: string; reference_id: string; start_ms: number; end_ms: number }; clip_sha256: string };
    adapter: { id: string; version: string }; mode: 'preview' | 'full'; paragraph_ids: string[];
    parameters: Record<string, never>;
  };
};
