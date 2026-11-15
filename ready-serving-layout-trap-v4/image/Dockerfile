FROM golang:1.22 AS builder
WORKDIR /src
COPY go.mod ./
COPY app ./app
RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/server ./app/cmd/server

FROM debian:bookworm-slim
WORKDIR /
RUN mkdir -p /app/config /srv/control /srv/layout
COPY --from=builder /out/server /usr/local/bin/server
COPY configs/default.yaml /app/config/default.yaml
COPY configs/routes.json /app/config/routes.json
COPY configs/runtime_policy.json /app/config/runtime_policy.json
COPY resources/control/startup_plan.json /srv/control/startup_plan.json
COPY resources/control/layout_stats.json /srv/control/layout_stats.json
COPY resources/control/slot_map.csv /srv/control/slot_map.csv
ADD resources/mixed.tar /srv/layout/
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/server"]
