GO ?= go
CARGO ?= cargo
.PHONY: test build run fmt vet rust-test rust-build

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
rust-test:
	$(CARGO) test --manifest-path rust/Cargo.toml --workspace
rust-build:
	$(CARGO) build --manifest-path rust/Cargo.toml --workspace --release
