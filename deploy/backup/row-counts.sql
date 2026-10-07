-- Точное число строк в каждой таблице приложения: по этой выборке учение по восстановлению
-- сверяет боевую БД и восстановленную (deploy/restore-drill.sh).
SELECT table_schema || '.' || table_name || ' ' ||
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;
