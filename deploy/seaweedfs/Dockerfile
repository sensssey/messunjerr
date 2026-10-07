# SeaweedFS для стенда и разработки (S4): официальный образ плюс socat.
#
# Зачем socat. Master, volume, filer и все их gRPC-порты SeaweedFS отдаёт без проверки прав, а у
# S3-шлюза есть ещё gRPC-порт (PutIdentity, PutPolicy: любой сосед по сети добавляет себе
# администратора), каталог Iceberg (8181) и Lance (9101). Права S3 (IAM) защищают только HTTP-порт
# S3. Поэтому всё слушает только 127.0.0.1, а наружу контейнера пробрасываются два порта S3
# (HTTP и HTTPS) сквозным TCP-пробросом: он не разбирает запросы, подпись SigV4 и TLS остаются
# между клиентом и SeaweedFS. Запуск пробросов и создание buckets: entrypoint.sh.
FROM chrislusf/seaweedfs:4.47

RUN apk add --no-cache socat
