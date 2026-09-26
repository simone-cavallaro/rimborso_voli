// PostgreSQL/WASM integration tests. Supabase auth/storage schemas are simulated;
// the migration, PostgreSQL RLS engine, constraints and triggers are real.
import { PGlite } from '@electric-sql/pglite';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const migration = await readFile(new URL('../../supabase/migrations/202609250001_requests_security.sql', import.meta.url), 'utf8');
const verification = await readFile(new URL('../../supabase/verify_security.sql', import.meta.url), 'utf8');
const preflight = await readFile(new URL('../../supabase/preflight.sql', import.meta.url), 'utf8');

for (const legacy of [false, true]) {
  test(`SQL security with ${legacy ? 'legacy bigint IDs and duplicate paths' : 'fresh UUID schema'}`, async () => {
    const db = new PGlite();
    try {
      await db.exec(`
        create role anon nologin;
        create role authenticated nologin;
        create schema auth;
        create schema storage;
        create table auth.users(id uuid primary key);
        insert into auth.users values ('${A}'), ('${B}');
        create function auth.uid() returns uuid language sql stable as
          $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
        create function storage.foldername(name text) returns text[] language sql immutable as
          $$ select (string_to_array(name, '/'))[1:array_length(string_to_array(name, '/'), 1)-1] $$;
        create function storage.extension(name text) returns text language sql immutable as
          $$ select reverse(split_part(reverse(name), '.', 1)) $$;
        create table storage.buckets(id text primary key, name text, public boolean, file_size_limit bigint, allowed_mime_types text[]);
        create table storage.objects(id uuid primary key default gen_random_uuid(), bucket_id text, name text);
        alter table storage.objects enable row level security;
        grant usage on schema auth, storage, public to anon, authenticated;
        grant select, insert, update, delete on storage.objects to anon, authenticated;
        create policy old_broad_storage_policy on storage.objects for all to public using (true) with check (true);
      `);
      if (legacy) {
        await db.exec(`
          create table public.richieste(
            id bigserial primary key, user_id uuid not null, created_at timestamptz default now(),
            compagnia_aerea text, numero_volo text, aeroporto_partenza text, aeroporto_destinazione text,
            data_acquisto text not null, data_volo text not null, costo_tratta numeric not null,
            pdf_ricevuta text not null, pdf_imbarco text not null
          );
          insert into public.richieste(user_id,data_acquisto,data_volo,costo_tratta,pdf_ricevuta,pdf_imbarco)
          values ('${A}','01/09/2026','01/10/2026',0,'${A}/legacy.pdf','${A}/legacy_boarding.pdf'),
                 ('${A}','01/09/2026','01/10/2026',40,'${A}/legacy.pdf','${A}/legacy_boarding.pdf');
          create policy old_broad_table_policy on public.richieste for all to public using (true) with check (true);
        `);
      }
      if (legacy) {
        const before = (await db.query(preflight)).rows[0].preflight;
        assert.equal(before.request_counts.total, 2);
        assert.equal(before.request_counts.invalid_cost, 1);
      }
      await db.exec(migration);
      await db.exec(migration); // Must be safe to re-apply.
      await db.exec(verification);
      const inspected = (await db.query(preflight)).rows[0].preflight;
      assert.equal(inspected.row_security.enabled, true);
      assert.equal(inspected.bucket.public, false);
      const bucket = (await db.query("select * from storage.buckets where id='pdf_rimborsi'")).rows[0];
      assert.equal(bucket.public, false);
      assert.equal(Number(bucket.file_size_limit), 15728640);
      assert.deepEqual(bucket.allowed_mime_types, ['application/pdf']);

      async function as(role, id, fn) {
        await db.query("select set_config('request.jwt.claim.sub', $1, false)", [id || '']);
        await db.exec(`set role ${role}`);
        try { return await fn(); }
        finally { await db.exec('reset role'); }
      }
      const receipt = `${A}/request/receipt.pdf`;
      let row;
      await as('authenticated', A, async () => {
        row = (await db.query('insert into public.richieste(user_id,pdf_ricevuta) values ($1,$2) returning *', [A, receipt])).rows[0];
        assert.equal(row.pdf_imbarco, null);
        assert.equal(row.costo_tratta, null);
        assert.equal(row.version, 1);
        assert.ok(row.client_request_id);
        await assert.rejects(db.query('insert into public.richieste(user_id,pdf_ricevuta) values ($1,$2)', [B, `${B}/receipt.pdf`]));
        await assert.rejects(db.query('insert into public.richieste(user_id,pdf_ricevuta) values ($1,$2)', [A, `${B}/receipt.pdf`]));
        await assert.rejects(db.query('insert into public.richieste(user_id,pdf_ricevuta) values ($1,$2)', [A, `${A}/../receipt.pdf`]));
        await assert.rejects(db.query('update public.richieste set costo_tratta=-1 where id=$1', [row.id]));
        await assert.rejects(db.query('update public.richieste set user_id=$1 where id=$2', [B, row.id]));
        await assert.rejects(db.query('update public.richieste set created_at=now() where id=$1', [row.id]));
        const edited = (await db.query('update public.richieste set pdf_imbarco=$1, costo_tratta=45.99 where id=$2 and version=1 returning *', [`${A}/boarding.pdf`, row.id])).rows[0];
        assert.equal(edited.version, 2);
        const summary = (await db.query('select public.richieste_summary() as summary')).rows[0].summary;
        assert.equal(Number(summary.totale_speso), legacy ? 85.99 : 45.99);
        const stale = await db.query('update public.richieste set costo_tratta=50 where id=$1 and version=1 returning id', [row.id]);
        assert.equal(stale.rows.length, 0);
        await db.query("insert into storage.objects(bucket_id,name) values ('pdf_rimborsi',$1)", [receipt]);
        await assert.rejects(db.query("insert into storage.objects(bucket_id,name) values ('pdf_rimborsi',$1)", [`${B}/receipt.pdf`]));
        await assert.rejects(db.query("insert into storage.objects(bucket_id,name) values ('pdf_rimborsi',$1)", [`${A}/payload.html`]));
        const overwritten = await db.query("update storage.objects set name=$1 where name=$2 returning id", [`${A}/changed.pdf`, receipt]);
        assert.equal(overwritten.rows.length, 0);
        const referenced = await db.query('delete from storage.objects where name=$1 returning id', [receipt]);
        assert.equal(referenced.rows.length, 0);
        assert.equal((await db.query('select * from storage.objects where name=$1', [receipt])).rows.length, 1);
      });
      await as('authenticated', B, async () => {
        assert.equal((await db.query('select public.richieste_summary() as summary')).rows[0].summary.numero_richieste, 0);
        assert.equal((await db.query('select * from public.richieste')).rows.length, 0);
        assert.equal((await db.query('select * from storage.objects')).rows.length, 0);
        assert.equal((await db.query('update public.richieste set costo_tratta=5 where id=$1 returning id', [row.id])).rows.length, 0);
        assert.equal((await db.query('delete from public.richieste where id=$1 returning id', [row.id])).rows.length, 0);
        assert.equal((await db.query('delete from storage.objects where name=$1 returning id', [receipt])).rows.length, 0);
      });
      await as('anon', null, async () => {
        await assert.rejects(db.query('select public.richieste_summary()'));
        await assert.rejects(db.query('select * from public.richieste'));
        assert.equal((await db.query('select * from storage.objects')).rows.length, 0);
        await assert.rejects(db.query("insert into storage.objects(bucket_id,name) values ('pdf_rimborsi',$1)", [receipt]));
      });
      await as('authenticated', A, async () => {
        await db.query('delete from public.richieste where id=$1', [row.id]);
        assert.equal((await db.query('delete from storage.objects where name=$1 returning id', [receipt])).rows.length, 1);
        if (legacy) {
          // Existing records keep their IDs/paths and can be repaired through editing.
          const existing = (await db.query('select * from public.richieste order by id')).rows;
          assert.equal(existing.length, 2);
          assert.equal(existing[0].pdf_ricevuta, `${A}/legacy.pdf`);
          await db.query("insert into storage.objects(bucket_id,name) values ('pdf_rimborsi',$1)", [`${A}/legacy.pdf`]);
          await db.query('delete from public.richieste where id=$1', [existing[0].id]);
          assert.equal((await db.query('delete from storage.objects where name=$1 returning id', [`${A}/legacy.pdf`])).rows.length, 0);
          await db.query('update public.richieste set costo_tratta=60 where id=$1', [existing[1].id]);
        }
      });
    } finally {
      await db.close();
    }
  });
}
