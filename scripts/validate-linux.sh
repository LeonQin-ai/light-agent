#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test "$(uname -s)" = Linux
go version
cargo --version
export CGO_ENABLED=1
go vet ./...
go test -race -p 2 ./... -timeout 120s
mkdir -p bin
go build -o bin/light-agentd ./control/cmd/light-agentd
cargo test --locked --manifest-path rust/Cargo.toml --workspace -j 2
cargo build --locked --manifest-path rust/Cargo.toml --workspace --release -j 2
printf 'Linux unit, race, static and release-build checks passed.\n'
