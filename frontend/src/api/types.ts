export type JobStatus = "queued" | "running" | "awaiting_criteria" | "completed" | "failed" | "cancelled";

export interface JobSummary {
  papers: number | null;
  final: number | null;
  judged: number | null;
  seeds: number | null;
  cost_usd: number | null;
}

export interface Job {
  id: string;
  title: string;
  mode: "topic" | "pdf";
  topic: string | null;
  n_seeds: number;
  seed_files: string[];
  review_criteria: boolean;
  max_cost_usd: number;
  status: JobStatus;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  stage: string | null;
  summary: JobSummary;
}

export interface Limits {
  max_cost_usd: number;
  max_pdfs: number;
  max_pdf_mb: number;
  max_seeds: number;
}

export interface Stage {
  key: string;
  index: number;
  stages: { key: string; label: string }[];
}

export interface Thresholds {
  drop: number;
  rel: number;
  core: number;
  source: string;
  knee: number | null;
}

export interface SeedPanel {
  name: string;
  active: boolean;
  frontier_max: number | null;
  processed: number;
  relevant: number;
  yield: number[];
  levels: { direction: string; depth: number; n: number; auc: number; rel_rate?: number }[];
}

export interface Metrics {
  state?: string;
  updated?: number;
  cost_usd?: number;
  thresholds?: Thresholds;
  null?: { q95: number; q99: number; mean: number };
  papers?: number;
  segments?: Record<"core" | "candidate" | "boundary" | "discard", number>;
  hist?: { edges: number[]; observed: number[]; null: number[] };
  mixture?: { grid: number[]; components: number[][] };
  calibration?: { labels: number; relevant: number; points: [number, number][]; curve: number[] };
  recapture?: { relevant_found: number; recall_est: number | null; rediscovery: number | null };
  judged?: { total: number; relevant: number };
  promoted?: number;
  search_rounds?: number;
  given_up?: Record<string, string>;
  seeds?: Record<string, SeedPanel>;
  events?: { at: number; kind: string; rule: string; payload: Record<string, unknown> }[];
  validation?: Record<string, unknown> | null;
}

export interface Progress {
  stage: Stage;
  metrics: Metrics;
  log: string[];
}

export interface PaperRow {
  id: string;
  rank: number | null;
  title: string | null;
  year: number | null;
  venue: string | null;
  citations: number | null;
  score: number | null;
  segment: string | null;
  llm_score: number | null;
  relevant: boolean | null;
  in_final: boolean | null;
  final_score: number | null;
  seed_origin: string | null;
  snippet: string | null;
  url: string | null;
  doi: string | null;
  arxiv: string | null;
  kind: "paper" | "repository";
}

export interface PaperDetail extends PaperRow {
  abstract: string | null;
  llm_reason: string | null;
  judged_as: string | null;
  similarity_to_seeds: number | null;
  similarity_to_topic: number | null;
  posterior: number | null;
  seed_reason: string | null;
  found_via: { direction: string; back: number; fwd: number; first: number; seed: string | null; parent_title: string | null }[];
  per_seed: { seed: string; cosine: number }[];
}

export interface PaperPage {
  total: number;
  page: number;
  page_size: number;
  items: PaperRow[];
}

export interface Seed {
  id: string;
  name: string;
  title: string;
  year: number | null;
  origin: string;
  reason: string | null;
  url: string | null;
  given_up: string | null;
}
