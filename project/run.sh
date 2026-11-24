#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
root="$PWD"
mkdir -p .local

# Use a short temporary path because Unix sockets have a path length limit.
if [[ ! -s .local/run-path ]] || [[ ! -d $(cat .local/run-path) ]]; then
    mktemp -d /tmp/cs631.XXXXXXXX > .local/run-path
fi
run=$(cat .local/run-path)
jobs=${JOBS:-8}

build() (
    local mode=$1
    local cppflags=${CPPFLAGS:-}
    local source
    source=$(mktemp -d "$run/postgres.XXXXXXXX")
    trap 'rm -rf -- "$source"' EXIT

    if [[ $mode == base ]]; then
        # Use the untouched PostgreSQL tree from the first commit.
        git archive "99d85d07f90101aa1b64fe853834fae4228ae9d9:postgres" | tar -xf - -C "$source"
    else
        # Copy the project's PostgreSQL source with the edits already in place.
        git -C postgres ls-files -z |
            rsync -lp --from0 --files-from=- postgres/ "$source/"
        mkdir -p "$source/project/extension"
        git -C project/extension ls-files -z |
            rsync -lp --from0 --files-from=- project/extension/ "$source/project/extension/"
    fi

    # Compile timing probes only into the diagnostic build.
    if [[ $mode == profile ]]; then
        cppflags="$cppflags -DCARD_FEEDBACK_PROFILE"
    fi
    mkdir -p "$run/$mode-src" "$run/$mode-made"
    # Keep modification times of unchanged files for incremental builds.
    rsync -rlpc --delete "$source/" "$run/$mode-src/"
    cd "$run/$mode-made"
    echo "Building $mode. Logs: $PWD"

    # Give each build its own install directory and use the same optimization level.
    if [[ ! -f GNUmakefile ]]; then
        "$run/$mode-src/configure" \
            --prefix="$run/$mode" \
            --enable-depend \
            --without-icu \
            --without-readline \
            --without-zlib \
            CFLAGS=-O2 \
            "CPPFLAGS=$cppflags" >configure.log 2>&1
    fi
    make -j"$jobs" >make.log 2>&1
    make install >install.log 2>&1

    # Use the matching pg_config so the extension links to the patched server.
    if [[ $mode != base ]]; then
        make -C "$run/$mode-src/project/extension" PG_CONFIG="$run/$mode/bin/pg_config" \
            install >extension.log 2>&1
    fi
    echo "$mode installed in $run/$mode"
)

# Keep one entry point for building, checking, and benchmarking.
case ${1:-} in
    build)
        if [[ -n ${2:-} ]]; then
            [[ $2 == base || $2 == feedback || $2 == profile ]]
            build "$2"
        else
            build base
            build feedback
        fi
        ;;
    review)
        exec python3 "$root/project/review_bench.py" "$run" "${2:-all}"
        ;;
    verify)
        exec python3 "$root/project/verify.py" "$run"
        ;;
    demo|job)
        exec python3 "$root/project/bench.py" "$run" "$@"
        ;;
    *)
        echo "Usage: bash project/run.sh {build [base|feedback|profile]|review [all|repeats|profile|timeouts|scope|transfer|pair]|verify|demo|job [quick|all]}"
        exit 1
        ;;
esac
