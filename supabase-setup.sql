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

-- Saves only if nobody else has saved since you last loaded (p_base).
-- If someone has, nothing is written and their version is returned instead.
create or replace function save_trip(p_key text, p_data jsonb, p_base timestamptz)
returns table (ok boolean, trip jsonb, saved_at timestamptz)
language plpgsql security definer set search_path = public as $$
declare cur trips;
begin
  select * into cur from trips where key = p_key for update;
  if not found then
    insert into trips (key, data) values (p_key, p_data) returning * into cur;
    return query select true, cur.data, cur.updated_at;
  elsif p_base is null or cur.updated_at > p_base then
    return query select false, cur.data, cur.updated_at;
  else
    update trips set data = p_data, updated_at = now() where key = p_key returning * into cur;
    return query select true, cur.data, cur.updated_at;
  end if;
end $$;

revoke all on function get_trip(text), save_trip(text, jsonb, timestamptz) from public;
grant execute on function get_trip(text), save_trip(text, jsonb, timestamptz) to anon, authenticated;
