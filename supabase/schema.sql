-- PayGuard Supabase schema. Apply manually in the Supabase SQL editor; the server never runs DDL
-- (Claude Desktop starts two server processes at once, so startup must not write).
-- Every statement is idempotent: re-running this file is safe.
--
-- `seq` is an insertion-order tiebreak for rows sharing a created_at (SQLite uses rowid for this).
-- Row level security is enabled with NO policies: the service key bypasses RLS, the anon key sees nothing.

create table if not exists purchases (
  purchase_id text primary key,
  principal_id text not null,
  task_id text,
  purchase_intent_id text not null,
  request_fingerprint text not null,
  attempt integer not null,
  supersedes_purchase_id text,
  approved_by text,
  service_id text not null,
  args jsonb not null,
  quoted_amount_minor bigint,
  authorized_amount_minor bigint,
  asset text,
  network text not null,
  pay_to text,
  payment_status text not null,
  fulfillment_status text not null,
  settlement_tx text,
  valid_before bigint,
  artifact_hash text,
  reject_reason text,
  history jsonb not null default '[]'::jsonb,
  created_at double precision not null,
  updated_at double precision not null,
  seq bigint generated always as identity,
  unique (principal_id, purchase_intent_id, request_fingerprint, attempt)
);
create index if not exists idx_purchases_fp on purchases (principal_id, request_fingerprint, created_at);
create index if not exists idx_purchases_principal_created on purchases (principal_id, created_at, seq);
create index if not exists idx_purchases_stuck on purchases (payment_status, updated_at);

create table if not exists evidence (
  evidence_id text primary key,
  purchase_id text not null references purchases (purchase_id) on delete cascade,
  kind text not null,
  payload jsonb not null,
  created_at double precision not null,
  seq bigint generated always as identity
);
create index if not exists idx_evidence_purchase on evidence (purchase_id, created_at);

create table if not exists findings (
  finding_id text primary key,
  purchase_id text not null references purchases (purchase_id) on delete cascade,
  code text not null,
  severity text not null,
  summary text not null,
  evidence_ids jsonb not null default '[]'::jsonb,
  safe_next_actions jsonb not null default '[]'::jsonb,
  created_at double precision not null,
  resolved_at double precision,
  seq bigint generated always as identity
);
create index if not exists idx_findings_purchase on findings (purchase_id);

alter table purchases enable row level security;
alter table evidence enable row level security;
alter table findings enable row level security;

-- Newer projects may not grant table privileges to service_role by default. GRANT is idempotent.
grant select, insert, update, delete on purchases, evidence, findings to service_role;
