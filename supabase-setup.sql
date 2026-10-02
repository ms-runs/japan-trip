-- Japan trip planner: shared saving.
-- Paste this whole file into Supabase > SQL Editor > New query, and press Run.
--
-- How it's locked down: the table itself is closed to the public (RLS on, no
-- policies). The page can only call the two functions below, and both need the
-- trip code from your share link. Without the code, nothing can be read or changed.

create table if not exists trips (
  key        text primary key check (length(key) between 12 and 100),
  data       jsonb not null,
  updated_at timestamptz not null default now()
);
alter table trips enable row level security;

create or replace function get_trip(p_key text)
returns table (trip jsonb, saved_at timestamptz)
language sql security definer set search_path = public as $$
  select data, updated_at from trips where key = p_key;
$$;

-- Saves only if nobody else has saved since you last loaded (p_base), and only if the
-- plan belongs to the same planner (tripId) as the one already stored under this code.
-- Otherwise nothing is written and the stored version is returned instead.
create or replace function save_trip(p_key text, p_data jsonb, p_base timestamptz)
returns table (ok boolean, trip jsonb, saved_at timestamptz)
language plpgsql security definer set search_path = public as $$
declare cur trips;
begin
  select * into cur from trips where key = p_key for update;
  if not found then
    insert into trips (key, data) values (p_key, p_data) returning * into cur;
    return query select true, cur.data, cur.updated_at;
  elsif (cur.data->>'tripId') is distinct from (p_data->>'tripId')
        or p_base is null or cur.updated_at > p_base then
    return query select false, cur.data, cur.updated_at;
  else
    update trips set data = p_data, updated_at = now() where key = p_key returning * into cur;
    return query select true, cur.data, cur.updated_at;
  end if;
end $$;

revoke all on function get_trip(text), save_trip(text, jsonb, timestamptz) from public;
grant execute on function get_trip(text), save_trip(text, jsonb, timestamptz) to anon, authenticated;

-- ── Booking PDFs (added later; safe to run this whole file again) ────────────
-- Files live beside the plan and need the same trip code. A file can only be
-- added to a plan that already exists, and each is capped at about 5 MB.
create table if not exists trip_files (
  key        text not null references trips (key) on delete cascade,
  id         text not null check (length(id) between 4 and 40),
  name       text,
  type       text,
  data       text not null check (length(data) < 7200000),   -- base64, ≈ 5 MB file
  created_at timestamptz not null default now(),
  primary key (key, id)
);
alter table trip_files enable row level security;

create or replace function save_file(p_key text, p_id text, p_name text, p_type text, p_data text)
returns boolean
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from trips where key = p_key) then return false; end if;
  insert into trip_files (key, id, name, type, data) values (p_key, p_id, p_name, p_type, p_data)
  on conflict (key, id) do update set name = excluded.name, type = excluded.type, data = excluded.data;
  return true;
end $$;

create or replace function get_file(p_key text, p_id text)
returns table (name text, type text, data text)
language sql security definer set search_path = public as $$
  select name, type, data from trip_files where key = p_key and id = p_id;
$$;

create or replace function delete_file(p_key text, p_id text)
returns boolean
language sql security definer set search_path = public as $$
  with gone as (delete from trip_files where key = p_key and id = p_id returning 1)
  select exists (select 1 from gone);
$$;

revoke all on function save_file(text, text, text, text, text), get_file(text, text), delete_file(text, text) from public;
grant execute on function save_file(text, text, text, text, text), get_file(text, text), delete_file(text, text) to anon, authenticated;
