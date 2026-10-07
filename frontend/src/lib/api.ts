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

export interface DatumCheck {
  datum_id: number;
  point_id: number;
  point_code?: string;
  declared_elevation_m: number;
  sigma_m: number;
  implied_candidate_elevation_m: number;
  discrepancy_m: number;
  tolerance_m: number;
  within_tolerance: boolean;
}

export type DatumAssessment = 'fills_datum_gap' | 'consistent' | 'contradiction_risk';

export interface DatumPrecheckResponse {
  read_only: boolean;
  notice: string;
  candidate: { point_code: string; point_id: number; elevation_m: number; sigma_m: number };
  assessment: DatumAssessment;
  component: {
    index: number;
    point_count: number;
    observation_count: number;
    datum_count: number;
    isolated: boolean;
  };
  existing_datums: DatumCheck[];
  conflicts: DatumCheck[];
  datum_already_on_point: boolean;
  draft_lock_version: number;
}
