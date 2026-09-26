-- Run in the Supabase SQL editor before deploying this version of the app.
-- Transactional and repeatable; does not delete requests or storage objects.
begin;

create table if not exists public.richieste (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id),
    created_at timestamptz not null default now(),
    compagnia_aerea text,
    numero_volo text,
    aeroporto_partenza text,
    aeroporto_destinazione text,
    data_acquisto text,
    data_volo text,
    costo_tratta numeric(12,2),
    pdf_ricevuta text,
    pdf_imbarco text
);

-- Preserve existing IDs, date column types and document paths.
alter table public.richieste
    add column if not exists client_request_id uuid not null default gen_random_uuid(),
    add column if not exists version integer not null default 1;
alter table public.richieste
    alter column pdf_imbarco drop not null,
    alter column data_acquisto drop not null,
    alter column data_volo drop not null,
    alter column costo_tratta drop not null;

create unique index if not exists richieste_client_request_id_uidx
    on public.richieste(client_request_id);
create index if not exists richieste_user_created_idx
    on public.richieste(user_id, created_at desc, id desc);
create index if not exists richieste_user_receipt_idx
    on public.richieste(user_id, pdf_ricevuta);
create index if not exists richieste_user_boarding_idx
    on public.richieste(user_id, pdf_imbarco);

create or replace function public.richieste_protect_identity()
returns trigger language plpgsql set search_path = '' as $$
begin
    if tg_op = 'UPDATE' then
        if new.id is distinct from old.id
           or new.user_id is distinct from old.user_id
           or new.client_request_id is distinct from old.client_request_id
           or new.created_at is distinct from old.created_at then
            raise exception 'Request identity cannot be changed';
        end if;
        new.version := old.version + 1;
    else
        new.version := 1;
    end if;
    return new;
end;
$$;
drop trigger if exists richieste_protect_identity on public.richieste;
create trigger richieste_protect_identity before insert or update on public.richieste
    for each row execute function public.richieste_protect_identity();

-- NOT VALID preserves legacy rows, while INSERT/UPDATE must satisfy these checks.
alter table public.richieste drop constraint if exists richieste_document_ownership;
alter table public.richieste add constraint richieste_document_ownership check (
    user_id is not null
    and pdf_ricevuta is not null
    and split_part(pdf_ricevuta, '/', 1) = user_id::text
    and pdf_ricevuta like '%.pdf'
    and pdf_ricevuta !~ '(^|/)\.\.(/|$)|//|\\'
    and (pdf_imbarco is null or (
        split_part(pdf_imbarco, '/', 1) = user_id::text
        and pdf_imbarco like '%.pdf'
        and pdf_imbarco !~ '(^|/)\.\.(/|$)|//|\\'
    ))
) not valid;
alter table public.richieste drop constraint if exists richieste_valid_cost;
alter table public.richieste add constraint richieste_valid_cost check (
    costo_tratta is null or (costo_tratta > 0 and costo_tratta <= 100000)
) not valid;

alter table public.richieste enable row level security;
alter table public.richieste force row level security;
revoke all on public.richieste from anon;
revoke all on public.richieste from authenticated;
grant select, insert, update, delete on public.richieste to authenticated;

-- Existing installations may use a serial/bigserial ID instead of UUID.
do $$
declare sequence_name text;
begin
    sequence_name := pg_get_serial_sequence('public.richieste', 'id');
    if sequence_name is not null then
        execute format('revoke all on sequence %s from anon', sequence_name);
        execute format('grant usage on sequence %s to authenticated', sequence_name);
    end if;
end;
$$;

-- The restrictive guard also blocks any pre-existing overly broad permissive policy.
drop policy if exists richieste_owner_guard on public.richieste;
create policy richieste_owner_guard on public.richieste as restrictive
    for all to public
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);
drop policy if exists richieste_owner_access on public.richieste;
create policy richieste_owner_access on public.richieste
    for all to authenticated
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

-- Aggregate all of the caller's rows, independently of API pagination limits.
create or replace function public.richieste_summary()
returns jsonb language sql stable security invoker set search_path = '' as $$
    select jsonb_build_object(
        'totale_speso', coalesce(sum(costo_tratta) filter (where costo_tratta > 0 and costo_tratta <= 100000), 0),
        'numero_richieste', count(*)
    )
    from public.richieste where user_id = (select auth.uid());
$$;
revoke all on function public.richieste_summary() from public, anon;
grant execute on function public.richieste_summary() to authenticated;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('pdf_rimborsi', 'pdf_rimborsi', false, 15728640, array['application/pdf'])
on conflict (id) do update set
    public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- Guards apply only to this bucket: policies for other buckets remain unchanged.
drop policy if exists pdf_rimborsi_owner_guard on storage.objects;
create policy pdf_rimborsi_owner_guard on storage.objects as restrictive
    for all to public
    using (bucket_id <> 'pdf_rimborsi' or (
        (select auth.uid()) is not null
        and (storage.foldername(name))[1] = (select auth.uid())::text
    ))
    with check (bucket_id <> 'pdf_rimborsi' or (
        (select auth.uid()) is not null
        and (storage.foldername(name))[1] = (select auth.uid())::text
        and storage.extension(name) = 'pdf'
        and name !~ '(^|/)\.\.(/|$)|//|\\'
    ));

drop policy if exists pdf_rimborsi_immutable on storage.objects;
create policy pdf_rimborsi_immutable on storage.objects as restrictive
    for update to public
    using (bucket_id <> 'pdf_rimborsi')
    with check (bucket_id <> 'pdf_rimborsi');

-- Protect referenced files, including files shared by duplicate legacy requests.
drop policy if exists pdf_rimborsi_keep_referenced on storage.objects;
create policy pdf_rimborsi_keep_referenced on storage.objects as restrictive
    for delete to public using (
        bucket_id <> 'pdf_rimborsi' or not exists (
            select 1 from public.richieste r
            where r.user_id = (select auth.uid())
              and (r.pdf_ricevuta = name or r.pdf_imbarco = name)
        )
    );

drop policy if exists pdf_rimborsi_select_own on storage.objects;
create policy pdf_rimborsi_select_own on storage.objects for select to authenticated
    using (bucket_id = 'pdf_rimborsi' and (storage.foldername(name))[1] = (select auth.uid())::text);
drop policy if exists pdf_rimborsi_insert_own on storage.objects;
create policy pdf_rimborsi_insert_own on storage.objects for insert to authenticated
    with check (bucket_id = 'pdf_rimborsi' and (storage.foldername(name))[1] = (select auth.uid())::text);
drop policy if exists pdf_rimborsi_delete_own on storage.objects;
create policy pdf_rimborsi_delete_own on storage.objects for delete to authenticated
    using (bucket_id = 'pdf_rimborsi' and (storage.foldername(name))[1] = (select auth.uid())::text);

commit;
