export function advancedPreference(): boolean {
  try { return localStorage.getItem('voice-advanced') === 'true'; } catch { return false; }
}
export function saveAdvanced(value: boolean) {
  try { localStorage.setItem('voice-advanced', String(value)); } catch { /* Optional view preference. */ }
}
export function adapterPreference(): string {
  try { return localStorage.getItem('voice-adapter') ?? ''; } catch { return ''; }
}
export function saveAdapter(value: string) {
  try { localStorage.setItem('voice-adapter', value); } catch { /* Optional model preference. */ }
}
