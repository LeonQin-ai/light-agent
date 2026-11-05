GO ?= go
.PHONY: test build run fmt vet
test:
	$(GO) test ./...
build:
	$(GO) build -o bin/light-agentd ./control/cmd/light-agentd
run:
	$(GO) run ./control/cmd/light-agentd
fmt:
	gofmt -w $$(find control -name '*.go')
vet:
	$(GO) vet ./...
