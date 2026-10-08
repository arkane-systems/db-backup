-- Test fixture for the PostgreSQL backup source. Runs once, as the postgres superuser.

CREATE ROLE app_owner LOGIN PASSWORD 'app-owner-pw';
CREATE ROLE app_reader LOGIN PASSWORD 'reader-pw';
CREATE ROLE reporting NOLOGIN;
GRANT reporting TO app_reader;

-- Least-privilege backup role: reads all data, but is not a superuser, so it
-- can't read role passwords (exercising the --no-role-passwords fallback).
CREATE ROLE dbbackup LOGIN PASSWORD 'dbbackup-pw' IN ROLE pg_read_all_data;

CREATE DATABASE app1 OWNER app_owner;
CREATE DATABASE "odd.name/db" OWNER app_owner;
CREATE DATABASE scratch;

\connect app1
SET ROLE app_owner;
CREATE SCHEMA billing;
CREATE TABLE public.customers (id serial PRIMARY KEY, name text NOT NULL, created timestamptz DEFAULT now());
INSERT INTO public.customers (name) SELECT 'customer ' || g FROM generate_series(1, 5000) g;
CREATE TABLE billing.invoices (
    id bigserial PRIMARY KEY,
    customer_id int REFERENCES public.customers (id),
    amount numeric(10, 2)
);
INSERT INTO billing.invoices (customer_id, amount) SELECT (g % 5000) + 1, g * 1.5 FROM generate_series(1, 20000) g;
CREATE INDEX invoices_customer ON billing.invoices (customer_id);
CREATE VIEW billing.big_invoices AS SELECT * FROM billing.invoices WHERE amount > 10000;
CREATE MATERIALIZED VIEW billing.totals AS SELECT customer_id, sum(amount) AS total FROM billing.invoices GROUP BY customer_id;
CREATE FUNCTION billing.invoice_count(c int) RETURNS bigint LANGUAGE sql AS $$ SELECT count(*) FROM billing.invoices WHERE customer_id = c $$;
GRANT USAGE ON SCHEMA billing TO reporting;
GRANT SELECT ON ALL TABLES IN SCHEMA billing TO reporting;
RESET ROLE;
ALTER DATABASE app1 SET work_mem = '8MB';
ANALYZE;

\connect "odd.name/db"
CREATE TABLE things (id int PRIMARY KEY, payload bytea);
INSERT INTO things SELECT g, decode(md5(g::text), 'hex') FROM generate_series(1, 1000) g;
ANALYZE;

\connect scratch
CREATE TABLE junk (x int);
