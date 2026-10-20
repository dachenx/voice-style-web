import type { TextDocument } from './TextPanel';
import type { ReferenceTranscript } from './ReferenceTranscript';

export type TaskInput = {
  request_id: string; expected_revision: number; expected_confirmation_id: string;
  expected_clip_id: string | null; adapter_id: string; mode: 'preview' | 'full';
  paragraph_id: string | null; parameters: Record<string, never>;
  expected_transcript_revision?: number;
  expected_adapter_version?: string;
  expected_config_fingerprint?: string;
};
export type Adapter = {
  id: string; version: string; label: string; is_simulated: boolean;
  reference_source?: 'persona'; max_text_characters?: number;
  requires_transcript?: boolean; model_id?: string; model_revision?: string;
  model_fingerprint?: string; config_fingerprint?: string;
  reference_max_duration_ms?: number; reference_min_sample_rate?: number;
  max_text_tokens?: number; limit_note?: string;
};
export type AdapterHealth = {
  adapter_id: string; configured: boolean; configuration_valid: boolean;
  execution_available: boolean; model_loaded: boolean; state: string;
  last_loaded_at?: string | null;
  error: { code: string; message: string } | null;
};
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
    reference: { id: string; filename: string; clip: { id: string; reference_id: string; start_ms: number; end_ms: number }; clip_sha256: string; transcript?: ReferenceTranscript };
    adapter: Adapter; mode: 'preview' | 'full'; paragraph_ids: string[];
    parameters: Record<string, never>;
  };
};
