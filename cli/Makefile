MODULE  := contained.dev/cli
BINARY  := contained
VERSION := $(shell \
  git describe --tags --exact-match 2>/dev/null | sed 's/^v//' | grep . \
  || git describe --tags --abbrev=7 2>/dev/null | sed 's/^v//' | grep . \
  || echo "dev")
LDFLAGS := -ldflags "-s -w -X $(MODULE)/internal/version.Version=$(VERSION)"
DIST    := dist

PLATFORMS := \
	linux/amd64 \
	linux/arm64 \
	darwin/amd64 \
	darwin/arm64 \
	windows/amd64

.PHONY: all build build-all release clean test vet generate

all: build

## generate — regenerate the embedded Python source tree (run after editing src/)
generate:
	go generate $(MODULE)/internal/pysource

## build — compile for the current platform
build: generate
	go build $(LDFLAGS) -o $(DIST)/$(BINARY) .

## build-all — cross-compile for all supported platforms
build-all: generate $(PLATFORMS)

$(PLATFORMS):
	$(eval OS   := $(word 1,$(subst /, ,$@)))
	$(eval ARCH := $(word 2,$(subst /, ,$@)))
	$(eval EXT  := $(if $(filter windows,$(OS)),.exe,))
	GOOS=$(OS) GOARCH=$(ARCH) CGO_ENABLED=0 \
		go build $(LDFLAGS) \
		-o $(DIST)/$(BINARY)_$(VERSION)_$(OS)_$(ARCH)$(EXT) .

## release — build all platforms + generate SHA-256 checksums
release: build-all
	cd $(DIST) && sha256sum $(BINARY)_$(VERSION)_* > checksums.txt
	@echo ""
	@echo "Release artefacts in $(DIST)/:"
	@ls -1 $(DIST)/

## test — run the test suite
test:
	go test ./...

## vet — run go vet
vet:
	go vet ./...

## clean — remove build artefacts
clean:
	rm -rf $(DIST)
