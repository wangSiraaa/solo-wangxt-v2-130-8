const API_BASE = '';

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  return response.json();
}

export interface Stage {
  name: string;
  status: string;
  attempt: number;
  detail: Record<string, unknown>;
}

export interface Job {
  id: number;
  status: string;
  current_stage: string;
  generation_key: string;
  snapshot_version: number;
  input_summary: Record<string, number | string>;
  algorithm: Record<string, unknown>;
  diagnostics: Record<string, any>;
  stages: Stage[];
}

export interface ResidualRow {
  line_code: string;
  observed_delta_m: number;
  adjusted_delta_m: number | null;
  correction_m: number | null;
  residual: number | null;
}

export type DatumPrecheckVerdict =
  | 'datumless_can_add'
  | 'compatible'
  | 'conflict'
  | 'existing_inconsistency'
  | 'indeterminate';

export interface DatumConflict {
  kind: 'same_point_datum' | 'network_implied_elevation' | 'existing_inconsistency';
  point_id?: number;
  point_code?: string | null;
  existing_datum_id?: number;
  declared_m?: number;
  existing_m?: number;
  implied_m?: number;
  residual_m?: number;
  threshold_m?: number;
  sigma_m?: number;
  status?: string;
  contradictions?: unknown[];
  anchor_datums?: Array<{ point_id: number; point_code: string | null; elevation_m: number }>;
}

export interface ExistingDatum {
  id?: number;
  point_id: number;
  point_code: string | null;
  elevation_m: number;
  sigma_m: number;
}

export interface DatumPrecheck {
  project_id: number;
  draft_lock_version: number;
  verdict: DatumPrecheckVerdict;
  risk_level: 'info' | 'warning' | 'danger';
  message: string;
  conflicts: DatumConflict[];
  existing_datums: ExistingDatum[];
  component: {
    index: number;
    point_count: number;
    observation_count: number;
    datum_count: number;
    sample_points: string[];
  };
  candidate: {
    point_id: number;
    point_code: string | null;
    elevation_m: number;
    sigma_m: number;
  };
  diagnostic: Record<string, any>;
}
