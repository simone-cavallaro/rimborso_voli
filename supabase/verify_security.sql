-- Read-only checks after migration. Inspect results in the SQL editor.
select relname, relrowsecurity, relforcerowsecurity
from pg_class where oid = 'public.richieste'::regclass;

select schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check
from pg_policies
where (schemaname = 'public' and tablename = 'richieste')
   or (schemaname = 'storage' and tablename = 'objects');

select id, public, file_size_limit, allowed_mime_types
from storage.buckets where id = 'pdf_rimborsi';

select column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public' and table_name = 'richieste'
order by ordinal_position;

-- Legacy anomalies should be repaired individually, never deleted automatically.
select count(*) as legacy_invalid_document_paths
from public.richieste
where user_id is null or pdf_ricevuta is null
   or split_part(pdf_ricevuta, '/', 1) <> user_id::text
   or (pdf_imbarco is not null and split_part(pdf_imbarco, '/', 1) <> user_id::text);

select count(*) as legacy_invalid_costs
from public.richieste where costo_tratta is not null
and not (costo_tratta > 0 and costo_tratta <= 100000);
