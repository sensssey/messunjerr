# PostgreSQL 18 с pgBackRest (S4-06). Один образ для сервиса `postgres` (archive_command вызывает
# pgbackrest) и для sidecar'а резервных копий, который работает с тем же каталогом данных.
FROM postgres:18

RUN apt-get update \
    && apt-get install -y --no-install-recommends pgbackrest \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o postgres -g postgres -m 0750 /etc/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest
