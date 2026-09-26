-- Read-only preflight. Save the returned JSON privately before the migration.
-- No document contents, user emails or individual travel records are returned.
select jsonb_build_object(
    'captured_at', now(),
    'server_version', current_setting('server_version'),
    'columns', (
        select coalesce(jsonb_agg(to_jsonb(c) order by c.ordinal_position), '[]'::jsonb)
        from information_schema.columns c
        where c.table_schema = 'public' and c.table_name = 'richieste'
    ),
    'row_security', (
        select jsonb_build_object('enabled', relrowsecurity, 'forced', relforcerowsecurity)
        from pg_class where oid = to_regclass('public.richieste')
    ),
    'policies', (
        select coalesce(jsonb_agg(to_jsonb(p)), '[]'::jsonb)
        from pg_policies p
        where (p.schemaname = 'public' and p.tablename = 'richieste')
           or (p.schemaname = 'storage' and p.tablename = 'objects')
    ),
    'table_grants', (
        select coalesce(jsonb_agg(to_jsonb(g)), '[]'::jsonb)
        from information_schema.role_table_grants g
        where g.table_schema = 'public' and g.table_name = 'richieste'
    ),
    'column_grants', (
        select coalesce(jsonb_agg(to_jsonb(g)), '[]'::jsonb)
        from information_schema.role_column_grants g
        where g.table_schema = 'public' and g.table_name = 'richieste'
    ),
    'constraints', (
        select coalesce(jsonb_agg(jsonb_build_object(
            'name', conname, 'validated', convalidated,
            'definition', pg_get_constraintdef(oid)
        )), '[]'::jsonb)
        from pg_constraint where conrelid = to_regclass('public.richieste')
    ),
    'indexes', (
        select coalesce(jsonb_agg(to_jsonb(i)), '[]'::jsonb)
        from pg_indexes i where i.schemaname = 'public' and i.tablename = 'richieste'
    ),
    'triggers', (
        select coalesce(jsonb_agg(jsonb_build_object(
            'name', tgname, 'definition', pg_get_triggerdef(oid)
        )), '[]'::jsonb)
        from pg_trigger where tgrelid = to_regclass('public.richieste') and not tgisinternal
    ),
    'existing_migration_functions', (
        select coalesce(jsonb_agg(jsonb_build_object(
            'name', p.proname, 'definition', pg_get_functiondef(p.oid)
        )), '[]'::jsonb)
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname in ('richieste_summary', 'richieste_protect_identity')
    ),
    'bucket', (
        select jsonb_build_object('id', id, 'public', public,
            'file_size_limit', file_size_limit, 'allowed_mime_types', allowed_mime_types)
        from storage.buckets where id = 'pdf_rimborsi'
    ),
    'request_counts', (
        select jsonb_build_object(
            'total', count(*),
            'missing_user', count(*) filter (where user_id is null),
            'missing_receipt', count(*) filter (where pdf_ricevuta is null or pdf_ricevuta = ''),
            'missing_boarding', count(*) filter (where pdf_imbarco is null or pdf_imbarco = ''),
            'invalid_cost', count(*) filter (where costo_tratta is not null and not (costo_tratta > 0 and costo_tratta <= 100000)),
            'wrong_receipt_owner', count(*) filter (where pdf_ricevuta is not null and split_part(pdf_ricevuta, '/', 1) <> user_id::text),
            'wrong_boarding_owner', count(*) filter (where pdf_imbarco is not null and split_part(pdf_imbarco, '/', 1) <> user_id::text)
        ) from public.richieste
    ),
    'storage_object_count', (
        select count(*) from storage.objects where bucket_id = 'pdf_rimborsi'
    )
) as preflight;
